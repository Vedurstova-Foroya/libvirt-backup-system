"""Tests for ``installer_password`` covering install + change-password wiring.

The module owns the orchestration around ``kopia_password``: idempotent
write-on-first-install, hard-fail on mismatch, and the rotation entry point
called by the CLI ``change-password`` subcommand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import installer_password, kopia_password, kopia_repo
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values["HOST_ID"] = "host-a"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    return cfg


def _password_path(cfg: Config) -> Path:
    return kopia_repo.password_file_path(cfg)


# --- install_password -----------------------------------------------------


def test_install_password_writes_file_when_flag_supplied_and_no_existing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    spec = kopia_password.PasswordSpec(literal="install-pw")
    assert installer_password.install_password(cfg, spec) == 0
    assert kopia_password.read_password_file(cfg) == "install-pw"


def test_install_password_no_flag_no_file_emits_usage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    assert installer_password.install_password(cfg, kopia_password.PasswordSpec()) == 1
    err = capsys.readouterr().err
    assert "kopia password missing" in err
    assert "--kopia-password-file" in err
    assert not _password_path(cfg).exists()


def test_install_password_no_flag_existing_file_keeps_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    pw_path = _password_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text("kept-across-runs\n", encoding="utf-8")
    pw_path.chmod(0o600)
    assert installer_password.install_password(cfg, kopia_password.PasswordSpec()) == 0
    # File untouched.
    assert pw_path.read_text(encoding="utf-8") == "kept-across-runs\n"


def test_install_password_idempotent_when_flag_matches_existing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    pw_path = _password_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text("same-pw\n", encoding="utf-8")
    pw_path.chmod(0o600)
    spec = kopia_password.PasswordSpec(literal="same-pw")
    assert installer_password.install_password(cfg, spec) == 0
    # File still mode 600 and value untouched.
    assert pw_path.read_text(encoding="utf-8") == "same-pw\n"


def test_install_password_fails_on_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    pw_path = _password_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text("existing\n", encoding="utf-8")
    pw_path.chmod(0o600)
    spec = kopia_password.PasswordSpec(literal="different")
    assert installer_password.install_password(cfg, spec) == 1
    err = capsys.readouterr().err
    assert "different value" in err
    assert "change-password" in err
    # File must remain untouched.
    assert pw_path.read_text(encoding="utf-8") == "existing\n"


def test_install_password_reports_resolution_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    # newline in literal triggers ValueError inside resolve_password.
    spec = kopia_password.PasswordSpec(literal="abc\nzap")
    assert installer_password.install_password(cfg, spec) == 1
    assert "kopia password resolution failed" in capsys.readouterr().err


def test_install_password_reports_resolution_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)

    def boom_resolve(*_a: object, **_k: object) -> str | None:
        raise OSError("disk read failed")

    monkeypatch.setattr(installer_password.kopia_password, "resolve_password", boom_resolve)
    assert installer_password.install_password(cfg, kopia_password.PasswordSpec()) == 1
    err = capsys.readouterr().err
    assert "kopia password resolution failed" in err
    assert "disk read failed" in err


def test_install_password_reports_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)

    def boom_write(_cfg: object, _value: str) -> None:
        raise OSError(13, "permission denied")

    monkeypatch.setattr(installer_password.kopia_password, "write_password_file", boom_write)
    spec = kopia_password.PasswordSpec(literal="install-pw")
    assert installer_password.install_password(cfg, spec) == 1
    err = capsys.readouterr().err
    assert "kopia password file write failed" in err
    assert "permission denied" in err


# --- change_password ------------------------------------------------------


def test_change_password_requires_a_password_spec(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    assert installer_password.change_password(cfg, kopia_password.PasswordSpec()) == 1
    err = capsys.readouterr().err
    assert "change-password requires" in err
    assert "--new-kopia-password" in err


def test_change_password_reports_resolution_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    spec = kopia_password.PasswordSpec(literal="bad\nvalue")
    assert installer_password.change_password(cfg, spec) == 1
    assert "kopia password resolution failed" in capsys.readouterr().err


def test_change_password_delegates_to_kopia_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(tmp_path)
    captured: dict[str, object] = {}

    def fake_change(config: Config, new_value: str) -> int:
        captured["config"] = config
        captured["value"] = new_value
        return 0

    monkeypatch.setattr(installer_password.kopia_password, "change_local_password", fake_change)
    spec = kopia_password.PasswordSpec(literal="rotated-pw")
    assert installer_password.change_password(cfg, spec) == 0
    assert captured["value"] == "rotated-pw"
    assert captured["config"] is cfg
