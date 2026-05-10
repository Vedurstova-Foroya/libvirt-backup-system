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


@pytest.fixture(autouse=True)
def _stub_domain_xml_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inactive backups call domain_xml_fingerprint, which would otherwise shell
    # out to virsh and explode in unit tests. Default to a stable stub so each
    # test only needs to override when it wants to assert the fingerprint code
    # path directly.
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: "fp-stub")


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
