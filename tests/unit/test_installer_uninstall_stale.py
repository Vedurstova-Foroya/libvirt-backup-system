"""Tests for stale kopia unit removal, ``_is_relative_to``, and purge-preserve paths."""

from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer_uninstall import (
    KOPIA_SYSTEMD_UNITS,
    _is_relative_to,
    remove_stale_kopia_units,
    resolve_purge_preserve_paths,
)

# -- remove_stale_kopia_units ------------------------------------------------


def test_remove_stale_kopia_units_removes_existing_files(tmp_path: Path) -> None:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    for name in KOPIA_SYSTEMD_UNITS:
        (systemd_dir / name).write_text("stale\n", encoding="utf-8")

    assert remove_stale_kopia_units(systemd_dir) is True

    for name in KOPIA_SYSTEMD_UNITS:
        assert not (systemd_dir / name).exists()


def test_remove_stale_kopia_units_ignores_missing_files(tmp_path: Path) -> None:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    # No unit files exist — all FileNotFoundError branches hit
    assert remove_stale_kopia_units(systemd_dir) is True


def test_remove_stale_kopia_units_returns_false_on_permission_error(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    for name in KOPIA_SYSTEMD_UNITS:
        (systemd_dir / name).write_text("stale\n", encoding="utf-8")

    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent == systemd_dir:
            raise PermissionError("permission denied")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)
    assert remove_stale_kopia_units(systemd_dir) is False


# -- _is_relative_to False branch -------------------------------------------


def test_is_relative_to_returns_false_for_unrelated_paths() -> None:
    assert _is_relative_to(Path("/etc/foo"), Path("/var/bar")) is False


# -- resolve_purge_preserve_paths with empty KOPIA_PASSWORD_FILE -------------


def test_resolve_purge_preserve_paths_skips_empty_password_file(tmp_path: Path) -> None:
    cfg = Config(
        values={**DEFAULTS, "KOPIA_PASSWORD_FILE": "", "BACKUP_PATH": "", "KOPIA_REPO_PATH": "", "HOST_ID": ""},
        path=tmp_path / "etc/config.env",
        prefix=tmp_path,
    )
    flags = {"config": True, "state": False, "logs": False}
    result = resolve_purge_preserve_paths(tmp_path, cfg, flags)
    assert result == []
