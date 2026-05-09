from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.storage import subpath_is_safe, unsafe_symlink_descendants


def test_unsafe_symlink_descendants_handles_missing_root(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    assert list(unsafe_symlink_descendants(root, root / "missing")) == []


def test_unsafe_symlink_descendants_allows_safe_file_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    vm_dir = root / "host/alpha"
    vm_dir.mkdir(parents=True)
    (vm_dir / "data").write_text("payload", encoding="utf-8")
    (vm_dir / "link").symlink_to("data")

    assert list(unsafe_symlink_descendants(root, vm_dir)) == []


def test_unsafe_symlink_descendants_rejects_paths_outside_backup_root(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    unsafe_link = outside_root / "link"
    unsafe_link.symlink_to(root / "target")

    assert list(unsafe_symlink_descendants(root, outside_root)) == [unsafe_link]


def test_subpath_is_safe_rejects_unrelated_path(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    assert not subpath_is_safe(root, tmp_path / "outside")
