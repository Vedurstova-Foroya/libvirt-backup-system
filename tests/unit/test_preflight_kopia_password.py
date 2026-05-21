"""Tests for ``preflight._validate_kopia_password_file``.

These exercise the kopia password-file mode/owner contract. The check
enforces ``0o600`` and (only when ``geteuid() == 0``) root ownership so a
non-root invocation does not reject a developer probe of a 600 root:root
file laid down by the install step.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import preflight
from tests.unit._preflight_helpers import make_config, write_password_file


def test_validate_kopia_password_file_empty_value(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["KOPIA_PASSWORD_FILE"] = "   "
    failures = preflight._validate_kopia_password_file(cfg)
    assert failures == ["KOPIA_PASSWORD_FILE must not be empty"]


def test_validate_kopia_password_file_missing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    failures = preflight._validate_kopia_password_file(cfg)
    assert any("KOPIA_PASSWORD_FILE missing" in failure for failure in failures)


def test_validate_kopia_password_file_lstat_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    target = preflight.prefixed(cfg.get("KOPIA_PASSWORD_FILE"), cfg.prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")

    real_lstat = Path.lstat

    def boom(self: Path) -> Any:
        if self == target:
            raise PermissionError("nope")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", boom)
    failures = preflight._validate_kopia_password_file(cfg)
    assert any("KOPIA_PASSWORD_FILE stat failed" in failure for failure in failures)


def test_validate_kopia_password_file_not_regular(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    target = preflight.prefixed(cfg.get("KOPIA_PASSWORD_FILE"), cfg.prefix)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir()  # directory, not a regular file
    target.chmod(0o600)
    failures = preflight._validate_kopia_password_file(cfg)
    assert any("is not a regular file" in failure for failure in failures)


def test_validate_kopia_password_file_wrong_mode(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    pw = write_password_file(cfg)
    pw.chmod(0o644)
    failures = preflight._validate_kopia_password_file(cfg)
    assert any("must be mode 600" in failure for failure in failures)


def test_validate_kopia_password_file_root_owner_check_skipped_when_non_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a non-root invocation must not reject a test-owned file.

    The install step lays the file down 600 root:root. ``preflight`` running
    as the operator (e.g. inside a non-root unit test) should accept it
    rather than fail every developer probe.
    """
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 1000)
    failures = preflight._validate_kopia_password_file(cfg)
    assert failures == []


def test_validate_kopia_password_file_root_owner_enforced_when_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)

    class FakeStat:
        def __init__(self, real: os.stat_result) -> None:
            self._real = real

        def __getattr__(self, name: str) -> Any:
            if name == "st_uid":
                return 1234
            return getattr(self._real, name)

    real_lstat = Path.lstat

    def fake_lstat(self: Path) -> Any:
        return FakeStat(real_lstat(self))

    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "lstat", fake_lstat)
    failures = preflight._validate_kopia_password_file(cfg)
    assert any("must be owned by root" in failure for failure in failures)
