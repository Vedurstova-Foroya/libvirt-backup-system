from __future__ import annotations

from libvirt_backup_system import backup
from libvirt_backup_system.config import Config
from libvirt_backup_system.vm_snapshot import FrozenSnapshot

from .test_backup import FakeSnapper, _disk_target, _install_stubs, _vm


def test_backup_writes_meta_snapshot_before_committing_overlays(monkeypatch, backup_config: Config) -> None:
    captured = _install_stubs(monkeypatch)

    class AssertingSnapper(FakeSnapper):
        def commit(self, snapshot: FrozenSnapshot) -> None:
            assert captured["create_path"], "meta snapshot must be written before blockcommit"
            super().commit(snapshot)

    snapper = AssertingSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
