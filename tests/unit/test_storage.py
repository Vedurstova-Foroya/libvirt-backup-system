from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.inactive_markers import stamp_is_safe
from libvirt_backup_system.storage import subpath_is_safe


def test_subpath_is_safe_rejects_unrelated_path(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    assert not subpath_is_safe(root, tmp_path / "outside")


def test_subpath_is_safe_rejects_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir()

    assert not subpath_is_safe(root, root)


def test_subpath_is_safe_rejects_real_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    assert not subpath_is_safe(root, root / "link" / "vm")


@pytest.mark.parametrize("stamp", ["20260520T123456", "copy-1"])
def test_stamp_is_safe_accepts_plain_names(stamp: str) -> None:
    assert stamp_is_safe(stamp)


@pytest.mark.parametrize("stamp", ["", ".", "..", ".hidden", "../escape", "a/b", "a\\b", "bad\nname", "bad\x00name"])
def test_stamp_is_safe_rejects_path_shapes(stamp: str) -> None:
    assert not stamp_is_safe(stamp)
