from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true"})
    return cfg


def test_backup_vm_refuses_existing_destination(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp"
    dest.mkdir(parents=True)
    (dest / "prior-backup").write_bytes(b"do-not-touch")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise AssertionError("backup must not run when destination exists")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert (dest / "prior-backup").exists()
    assert "backup destination already exists" in capsys.readouterr().err
