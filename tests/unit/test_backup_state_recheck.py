from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.run_records import CheckpointReadError
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true", "INACTIVE_COPY_EVERY_RUN": "false"})
    return cfg


def test_backup_running_fails_when_checkpoint_metadata_unreadable(monkeypatch, capsys, backup_config) -> None:
    # Regression: an unreadable .cpt at backup start would otherwise let
    # virtnbdbackup run against a stale baseline and record_run silently skip
    # the missing diff. Refuse to start so the operator sees the I/O problem.
    cfg = _backup_config(backup_config)

    def boom(chain_dir, vm_name):
        del chain_dir, vm_name
        raise CheckpointReadError("permission denied")

    monkeypatch.setattr("libvirt_backup_system.backup.list_checkpoints", boom)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run virtnbdbackup")),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "checkpoint metadata read failed" in capsys.readouterr().err


def test_backup_vm_fails_when_vm_starts_mid_copy(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.domain_state", lambda cfg, name: "running")
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    written: list[tuple[str, str]] = []

    def fake_write(marker, stamp, fingerprint, vm):
        written.append((stamp, fingerprint))
        return True

    monkeypatch.setattr("libvirt_backup_system.backup.write_marker", fake_write)

    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "inactive backup not trusted: VM is no longer shut off" in err
    assert written == []


def test_backup_vm_fails_when_domstate_recheck_fails(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.domain_state", lambda cfg, name: None)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "inactive backup not trusted: VM is no longer shut off" in err
