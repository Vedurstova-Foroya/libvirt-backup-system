from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM


def _backup_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
        }
    )
    return cfg


def test_backup_vm_refuses_existing_destination(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / "backups/host/alpha/2026-05/stamp"
    dest.mkdir(parents=True)
    (dest / "prior-backup").write_bytes(b"do-not-touch")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise AssertionError("backup must not run when destination exists")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert (dest / "prior-backup").exists()
    assert "backup destination already exists" in capsys.readouterr().err


def test_backup_vm_uses_copy_only_for_shut_off(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)

    assert backup_vm(cfg, VM("alpha", "paused"), "2026-05", "s1")
    assert "full" in calls[-1]
    assert "copy" not in calls[-1]
    paused_marker = tmp_path / "backups/host/alpha/2026-05/.inactive-copy-complete"
    assert not paused_marker.exists()

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "s2")
    assert "copy" in calls[-1]
    shutoff_marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    assert shutoff_marker.exists()
