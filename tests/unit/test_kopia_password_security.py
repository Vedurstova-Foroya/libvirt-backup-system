"""Tests for ``kopia_password`` covering security-failure cleanup,
temporary_password_file edge cases, and read_secure_password_file paths.

Split from ``test_kopia_password.py`` to stay within the 300-line limit.
"""

from __future__ import annotations

import os
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


# --- read_secure_password_file: OSError on lstat (lines 116-117) ------------


def test_read_secure_password_file_oserror_on_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lstat() raises a non-FileNotFoundError OSError, the function
    falls through to password_file_security_failure and raises PermissionError
    (not FileNotFoundError)."""
    cfg = _make_config(tmp_path)
    pw = _write_password(cfg, value="good-pw")

    real_lstat = Path.lstat
    call_count = 0

    def oserror_on_first_lstat(self: Path) -> os.stat_result:
        nonlocal call_count
        # read_secure_password_file calls lstat once, then
        # password_file_security_failure calls lstat again.
        call_count += 1
        if call_count == 1 and self == pw:
            raise OSError("device not ready")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", oserror_on_first_lstat)
    # The second lstat (inside password_file_security_failure) succeeds,
    # so the file passes the security check and returns the value.
    assert kopia_password.read_secure_password_file(pw) == "good-pw"


def test_read_secure_password_file_oserror_on_lstat_then_security_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lstat() raises OSError and the security check also fails,
    the function raises PermissionError (not FileNotFoundError) because
    ``missing`` stays False."""
    cfg = _make_config(tmp_path)
    pw = _write_password(cfg, value="good-pw")
    pw.chmod(0o644)  # make security check fail

    real_lstat = Path.lstat
    call_count = 0

    def oserror_on_first_lstat(self: Path) -> os.stat_result:
        nonlocal call_count
        call_count += 1
        if call_count == 1 and self == pw:
            raise OSError("device not ready")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", oserror_on_first_lstat)
    with pytest.raises(PermissionError, match="must be mode 600"):
        kopia_password.read_secure_password_file(pw)


def test_allowed_password_owner_uids_without_geteuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(kopia_password.os, "geteuid")

    assert kopia_password._allowed_password_owner_uids() == {0}


# --- write_password_file: security-failure cleanup (lines 151-152) ----------


def test_write_password_file_security_failure_cleans_up_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the security check on the temp file fails *after* writing,
    the temp file must be removed and PermissionError raised."""
    cfg = _make_config(tmp_path)
    pw_path = kopia_repo.password_file_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)

    # Force password_file_security_failure to report a failure for the tmp file.
    real_security_check = kopia_password.password_file_security_failure

    def fail_for_tmp(path: Path, *, label: str = "kopia password file") -> str | None:
        if ".tmp" in path.name:
            return f"{label} is not a regular file: {path}"
        return real_security_check(path, label=label)

    monkeypatch.setattr(kopia_password, "password_file_security_failure", fail_for_tmp)
    with pytest.raises(PermissionError, match="is not a regular file"):
        kopia_password.write_password_file(cfg, "some-pw")
    # No stray .tmp files should remain.
    leftovers = [child for child in pw_path.parent.iterdir() if child.name.startswith(".")]
    assert leftovers == []


# --- temporary_password_file: empty value (line 162) ------------------------


def test_temporary_password_file_rejects_empty_value(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with (
        pytest.raises(ValueError, match="kopia password must not be empty"),
        kopia_password.temporary_password_file(cfg, ""),
    ):
        pass  # pragma: no cover


# --- temporary_password_file: chown failure (lines 175-179) -----------------


def test_temporary_password_file_chown_failure_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When running as root, a chown failure inside temporary_password_file
    must remove the temp file and raise OSError."""
    cfg = _make_config(tmp_path)
    pw_dir = kopia_repo.password_file_path(cfg).parent
    pw_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(kopia_password.os, "geteuid", lambda: 0)

    def boom_chown(_path: object, _uid: int, _gid: int) -> None:
        raise PermissionError("chown denied")

    monkeypatch.setattr(kopia_password.os, "chown", boom_chown)
    with (
        pytest.raises(OSError, match="chown root:root failed"),
        kopia_password.temporary_password_file(cfg, "verify-pw"),
    ):
        pass  # pragma: no cover
    # No stray .tmp files should remain.
    leftovers = [child for child in pw_dir.iterdir() if child.name.startswith(".")]
    assert leftovers == []


# --- temporary_password_file: security-failure cleanup (lines 182-183) ------


def test_temporary_password_file_security_failure_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the security check on the temp file fails after writing,
    the temp file must be removed and PermissionError raised."""
    cfg = _make_config(tmp_path)
    pw_dir = kopia_repo.password_file_path(cfg).parent
    pw_dir.mkdir(parents=True, exist_ok=True)

    real_security_check = kopia_password.password_file_security_failure

    def fail_for_tmp(path: Path, *, label: str = "kopia password file") -> str | None:
        if ".tmp" in path.name:
            return f"{label} is not a regular file: {path}"
        return real_security_check(path, label=label)

    monkeypatch.setattr(kopia_password, "password_file_security_failure", fail_for_tmp)
    with (
        pytest.raises(PermissionError, match="is not a regular file"),
        kopia_password.temporary_password_file(cfg, "verify-pw"),
    ):
        pass  # pragma: no cover
    # No stray .tmp files should remain.
    leftovers = [child for child in pw_dir.iterdir() if child.name.startswith(".")]
    assert leftovers == []
