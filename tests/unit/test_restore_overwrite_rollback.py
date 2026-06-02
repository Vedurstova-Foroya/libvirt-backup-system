from __future__ import annotations

from pathlib import Path

from libvirt_backup_system import restore


def test_rollback_removes_new_disk_when_original_destination_was_absent(tmp_path: Path) -> None:
    dest = tmp_path / "missing-before-restore.qcow2"
    temp = tmp_path / ".missing-before-restore.qcow2.vda.restore.tmp"
    temp.write_bytes(b"new")
    backup_map = restore.replace_overwrite_disks_with_backups({"vda": temp}, {"vda": dest})
    assert backup_map is not None
    assert dest.read_bytes() == b"new"
    restore.rollback_overwrite_disks(backup_map, {"vda": dest})
    assert not dest.exists()
