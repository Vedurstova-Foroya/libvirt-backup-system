from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.storage import subpath_is_safe


def test_subpath_is_safe_rejects_unrelated_path(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    assert not subpath_is_safe(root, tmp_path / "outside")
