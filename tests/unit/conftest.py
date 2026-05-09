from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config


@pytest.fixture
def backup_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": "host",
        }
    )
    return cfg


def symlink_backup_vm_path(tmp_path: Path, symlink_case: str) -> Path:
    if symlink_case == "host":
        outside_host = tmp_path / "outside/host"
        outside_host.mkdir(parents=True)
        (tmp_path / "backups/host").symlink_to(outside_host, target_is_directory=True)
        return outside_host / "alpha"

    outside_vm = tmp_path / "outside/alpha"
    outside_vm.mkdir(parents=True)
    (tmp_path / "backups/host").mkdir()
    (tmp_path / "backups/host/alpha").symlink_to(outside_vm, target_is_directory=True)
    return outside_vm
