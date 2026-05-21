"""Tests for ``kopia_password`` covering the resolve/read/write paths.

Together with ``test_installer_password`` and ``test_kopia_password_rotate``
these pin down the contract the installer relies on: resolution honours all
four flag forms, on-disk reads reject loose modes, atomic writes land at mode
600, and rotation rolls back only the steps that are individually safe to
roll back.
"""

from __future__ import annotations

import os
import stat as _stat
from pathlib import Path

import pytest

from libvirt_backup_system import kopia_password, kopia_repo
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values["HOST_ID"] = "host-a"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    return cfg


def _write_password(cfg: Config, value: str = "swordfish") -> Path:
    path = kopia_repo.password_file_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


# --- resolve_password -------------------------------------------------------


def test_resolve_password_returns_none_when_nothing_set() -> None:
    assert kopia_password.resolve_password(kopia_password.PasswordSpec()) is None


def test_resolve_password_literal_returns_value() -> None:
    spec = kopia_password.PasswordSpec(literal="hunter2")
    assert kopia_password.resolve_password(spec) == "hunter2"


def test_resolve_password_literal_rejects_empty() -> None:
    spec = kopia_password.PasswordSpec(literal="")
    with pytest.raises(ValueError, match="must not be empty"):
        kopia_password.resolve_password(spec)


def test_resolve_password_literal_rejects_newline() -> None:
    spec = kopia_password.PasswordSpec(literal="abc\nzap")
    with pytest.raises(ValueError, match="newline"):
        kopia_password.resolve_password(spec)


def test_resolve_password_file_form_reads_trimmed_content(tmp_path: Path) -> None:
    pw_path = tmp_path / "shared.pw"
    pw_path.write_text("kopia-secret\n", encoding="utf-8")
    spec = kopia_password.PasswordSpec(file=str(pw_path))
    assert kopia_password.resolve_password(spec) == "kopia-secret"


def test_resolve_password_file_form_rejects_empty(tmp_path: Path) -> None:
    pw_path = tmp_path / "empty.pw"
    pw_path.write_text("\n", encoding="utf-8")
    spec = kopia_password.PasswordSpec(file=str(pw_path))
    with pytest.raises(ValueError, match="must not be empty"):
        kopia_password.resolve_password(spec)


def test_resolve_password_file_form_dash_reads_stdin() -> None:
    # ``-`` reads the password from stdin so config-management can pipe it.
    spec = kopia_password.PasswordSpec(file="-")
    assert kopia_password.resolve_password(spec, stdin=iter(["from-stdin\n"])) == "from-stdin"


def test_resolve_password_env_form_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBS_TEST_PASSWORD", "pass-from-env")
    spec = kopia_password.PasswordSpec(env_var="LBS_TEST_PASSWORD")
    assert kopia_password.resolve_password(spec) == "pass-from-env"


def test_resolve_password_env_form_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LBS_TEST_PASSWORD_MISSING", raising=False)
    spec = kopia_password.PasswordSpec(env_var="LBS_TEST_PASSWORD_MISSING")
    with pytest.raises(KeyError, match="LBS_TEST_PASSWORD_MISSING"):
        kopia_password.resolve_password(spec)


def test_resolve_password_env_form_empty_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LBS_EMPTY", "")
    spec = kopia_password.PasswordSpec(env_var="LBS_EMPTY")
    with pytest.raises(ValueError, match="must not be empty"):
        kopia_password.resolve_password(spec)


# --- read_password_file -----------------------------------------------------


def test_read_password_file_returns_trimmed_value(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="hunter2")
    assert kopia_password.read_password_file(cfg) == "hunter2"


def test_read_password_file_rejects_wrong_mode(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    pw = _write_password(cfg)
    pw.chmod(0o644)
    with pytest.raises(PermissionError, match="must be mode 600"):
        kopia_password.read_password_file(cfg)


def test_read_password_file_propagates_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        kopia_password.read_password_file(cfg)


# --- write_password_file ----------------------------------------------------


def test_write_password_file_atomic_mode_600(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    kopia_password.write_password_file(cfg, "fresh-pw")
    path = kopia_repo.password_file_path(cfg)
    assert path.read_text(encoding="utf-8") == "fresh-pw\n"
    assert _stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_password_file_rejects_empty_value(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        kopia_password.write_password_file(cfg, "")


def test_write_password_file_chown_failure_cleans_up_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When we run as root we chown the new file 0:0 before renaming into place.
    # A chown failure must surface as OSError and leave no stray ``.tmp`` file
    # behind in the password directory.
    cfg = _make_config(tmp_path)
    pw_path = kopia_repo.password_file_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(kopia_password.os, "geteuid", lambda: 0)

    def boom_chown(_path: object, _uid: int, _gid: int) -> None:
        raise PermissionError("chown denied")

    monkeypatch.setattr(kopia_password.os, "chown", boom_chown)
    with pytest.raises(OSError, match="chown root:root failed"):
        kopia_password.write_password_file(cfg, "fresh-pw")
    leftovers = [child for child in pw_path.parent.iterdir() if child.name.startswith(".")]
    assert leftovers == []


def test_write_password_file_chowns_root_when_running_as_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_password.os, "geteuid", lambda: 0)
    chowned: list[tuple[object, int, int]] = []

    def fake_chown(path: object, uid: int, gid: int) -> None:
        chowned.append((path, uid, gid))

    monkeypatch.setattr(kopia_password.os, "chown", fake_chown)
    kopia_password.write_password_file(cfg, "rooted-pw")
    assert chowned and chowned[0][1:] == (0, 0)


# --- existing_password_matches ---------------------------------------------


def test_existing_password_matches_returns_true(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="match")
    assert kopia_password.existing_password_matches(cfg, "match") is True


def test_existing_password_matches_returns_false(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="other")
    assert kopia_password.existing_password_matches(cfg, "match") is False


def test_existing_password_matches_returns_false_when_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert kopia_password.existing_password_matches(cfg, "anything") is False


# --- password_file_is_secure -----------------------------------------------


def test_password_file_is_secure_returns_true_for_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "kopia.pw"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o600)
    assert kopia_password.password_file_is_secure(path) is True


def test_password_file_is_secure_returns_false_when_missing(tmp_path: Path) -> None:
    assert kopia_password.password_file_is_secure(tmp_path / "absent.pw") is False


def test_password_file_is_secure_returns_false_when_not_mode_600(tmp_path: Path) -> None:
    path = tmp_path / "kopia.pw"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o644)
    assert kopia_password.password_file_is_secure(path) is False


def test_password_file_is_secure_returns_false_when_directory(tmp_path: Path) -> None:
    path = tmp_path / "kopia-dir"
    path.mkdir()
    path.chmod(0o600)
    assert kopia_password.password_file_is_secure(path) is False


def test_password_file_is_secure_returns_false_for_non_root_owner_when_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "kopia.pw"
    path.write_text("x", encoding="utf-8")
    path.chmod(0o600)

    real_lstat = Path.lstat

    class FakeStat:
        def __init__(self, real: os.stat_result) -> None:
            self._real = real

        def __getattr__(self, name: str) -> object:
            if name == "st_uid":
                return 1234
            return getattr(self._real, name)

    def fake_lstat(self: Path) -> object:
        return FakeStat(real_lstat(self))

    monkeypatch.setattr(kopia_password.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    assert kopia_password.password_file_is_secure(path) is False
