from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true"})
    return cfg


def test_backup_vm_removes_partial_destination_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_bytes(b"junk")
        raise CommandError(CommandResult(args, 7, "", "boom"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert not dest.exists()
    err = capsys.readouterr()
    assert "removed partial backup" in err.out
    assert "backup failed" in err.err


def test_backup_vm_partial_backup_removal_failure_surfaced(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_bytes(b"junk")
        raise CommandError(CommandResult(args, 7, "", "boom"))

    def failing_rmtree(path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.shutil.rmtree", failing_rmtree)

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "partial backup removal failed" in err
    assert "permission denied" in err
    assert dest.exists()


def test_backup_vm_partial_backup_removal_incomplete_surfaced(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        raise CommandError(CommandResult(args, 7, "", "boom"))

    def lying_rmtree(path: Path) -> None:
        return None

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.shutil.rmtree", lying_rmtree)

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "partial backup removal incomplete" in capsys.readouterr().err
    assert dest.exists()
