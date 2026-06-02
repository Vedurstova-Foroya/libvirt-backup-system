"""Tests for the ``BACKUP_PATH`` readonly and writable validators.

Covers the path-shape, existence, mount-probe, and subpath-safe branches in
both ``_validate_backup_path_readonly`` and ``_validate_backup_path_writable``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import preflight, preflight_backup_path
from tests.unit._preflight_helpers import make_config


def test_validate_backup_path_readonly_skips_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert preflight.validate_backup_path_readonly(cfg) == []


def test_validate_backup_path_readonly_rejects_relative(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = "relative/path"
    assert preflight.validate_backup_path_readonly(cfg) == ["BACKUP_PATH must be an absolute path"]


def test_validate_backup_path_readonly_must_exist(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = str(tmp_path / "missing")
    assert preflight.validate_backup_path_readonly(cfg) == ["BACKUP_PATH must exist"]


def test_validate_backup_path_readonly_must_be_dir(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    file_path = tmp_path / "notdir"
    file_path.write_text("x", encoding="utf-8")
    cfg.values["BACKUP_PATH"] = str(file_path)
    assert preflight.validate_backup_path_readonly(cfg) == ["BACKUP_PATH must be a directory"]


def test_validate_backup_path_readonly_must_be_mount(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    failures = preflight.validate_backup_path_readonly(cfg)
    assert failures == ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]


def test_validate_backup_path_readonly_mount_probe_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"

    def boom(self: Path) -> bool:
        raise OSError("ESTALE")

    monkeypatch.setattr(Path, "is_mount", boom)
    failures = preflight.validate_backup_path_readonly(cfg)
    assert any("BACKUP_PATH mount probe failed: ESTALE" in failure for failure in failures)


def test_validate_backup_path_readonly_rejects_unsafe_subpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(preflight_backup_path, "subpath_is_safe", lambda *_a, **_kw: False)
    failures = preflight.validate_backup_path_readonly(cfg)
    assert failures == ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]


def test_validate_backup_path_readonly_mounted_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cover the (mounted=True, error=None) -> subpath_is_safe fall-through."""
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: (True, None))
    assert preflight.validate_backup_path_readonly(cfg) == []


def test_validate_backup_path_writable_skips_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert preflight.validate_backup_path_writable(cfg) == []


def test_validate_backup_path_writable_returns_readonly_failures_unchanged(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = str(tmp_path / "missing")
    assert preflight.validate_backup_path_writable(cfg) == ["BACKUP_PATH must exist"]


def test_validate_backup_path_writable_handles_mount_required(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    failures = preflight.validate_backup_path_writable(cfg)
    assert failures == ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]


def test_validate_backup_path_writable_mount_probe_error_inside_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "validate_backup_path_readonly", lambda _cfg: [])
    states = iter([(False, "ESTALE"), (False, "ESTALE")])
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: next(states))
    failures = preflight.validate_backup_path_writable(cfg)
    assert any("mount probe failed" in failure for failure in failures)


def test_validate_backup_path_writable_inner_mount_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "validate_backup_path_readonly", lambda _cfg: [])
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: (False, None))
    failures = preflight.validate_backup_path_writable(cfg)
    assert failures == ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]


def test_validate_backup_path_writable_unsafe_subpath_after_mkdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    calls = {"n": 0}

    def fake_safe(_root: Path, _path: Path) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # safe for readonly, unsafe inside writable

    monkeypatch.setattr(preflight_backup_path, "subpath_is_safe", fake_safe)
    failures = preflight.validate_backup_path_writable(cfg)
    assert failures == ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]


def test_validate_backup_path_writable_post_mkdir_mount_probe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "validate_backup_path_readonly", lambda _cfg: [])
    seq = iter([(True, None), (False, "lost mount")])
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: next(seq))
    failures = preflight.validate_backup_path_writable(cfg)
    assert any("mount probe failed: lost mount" in failure for failure in failures)


def test_validate_backup_path_writable_post_mkdir_unmounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "validate_backup_path_readonly", lambda _cfg: [])
    seq = iter([(True, None), (False, None)])
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: next(seq))
    failures = preflight.validate_backup_path_writable(cfg)
    assert failures == ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]


def test_validate_backup_path_writable_post_mkdir_mounted_falls_through_to_write_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover branch 233->235: post-mkdir mounted=True falls through to write probe."""
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr(preflight_backup_path, "backup_path_is_mount", lambda _p: (True, None))
    assert preflight.validate_backup_path_writable(cfg) == []


def test_validate_backup_path_writable_oserror_wraps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(_path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(preflight_backup_path, "write_probe", boom)
    failures = preflight.validate_backup_path_writable(cfg)
    assert any("BACKUP_PATH must be writable: permission denied" in failure for failure in failures)
