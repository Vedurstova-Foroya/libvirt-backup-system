from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import BETA_UUID


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true", "INACTIVE_COPY_EVERY_RUN": "false"})
    return cfg


def test_finalize_inactive_marker_fails_on_utime_failure(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    def failing_utime(path: object, times: tuple[float, float]) -> None:
        raise OSError("readonly mount")

    monkeypatch.setattr("libvirt_backup_system.backup.os.utime", failing_utime)
    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "inactive marker backdate failed; rolling back marker" in err
    marker = tmp_path / f"backups/host/{BETA_UUID}/2026-05/.inactive-copy-complete"
    assert not marker.exists()


def test_backup_vm_fails_and_cleans_up_when_marker_write_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    parent = tmp_path / f"backups/host/{BETA_UUID}/2026-05"

    def fail_open(path: Path) -> int:
        raise OSError("open denied")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers._open_excl_nofollow", fail_open)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    leftover_temps = [p for p in parent.iterdir() if p.name.startswith(".") and p.suffix == ".tmp"]
    assert leftover_temps == []
    assert "inactive marker write failed" in capsys.readouterr().err
