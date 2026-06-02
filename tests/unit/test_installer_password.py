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
from libvirt_backup_system.shell import CommandError, CommandResult


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values["HOST_ID"] = "host-a"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    return cfg


def _password_path(cfg: Config) -> Path:
    return kopia_repo.password_file_path(cfg)


def _create_local_repo_sentinel(cfg: Config) -> Path:
    repo_path = kopia_repo.local_repo_path(cfg)
    repo_path.mkdir(parents=True)
    (repo_path / "kopia.repository.f").write_text("repo\n", encoding="utf-8")
    return repo_path


# --- install_password -----------------------------------------------------


def test_install_password_writes_file_when_flag_supplied_and_no_existing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    spec = kopia_password.PasswordSpec(literal="install-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 0
    assert kopia_password.read_password_file(cfg) == "install-pw"


def test_install_password_first_install_requires_acknowledgement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    assert installer_password.install_password(cfg, kopia_password.PasswordSpec(literal="install-pw")) == 1
    err = capsys.readouterr().err
    assert "--acknowledge-password-loss" in err
    assert "install-pw" not in err
    assert not _password_path(cfg).exists()


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


def test_install_password_no_flag_existing_insecure_file_reports_security_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    pw_path = _password_path(cfg)
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text("loose\n", encoding="utf-8")
    pw_path.chmod(0o644)
    assert installer_password.install_password(cfg, kopia_password.PasswordSpec()) == 1
    err = capsys.readouterr().err
    assert "kopia password file security failure" in err
    assert "must be mode 600" in err


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


def test_install_password_existing_repo_rejects_bad_supplied_password_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    repo_path = _create_local_repo_sentinel(cfg)

    def fail_connect(**kwargs: object) -> None:
        assert kwargs["repo_path"] == repo_path
        password_file = kwargs["password_file"]
        assert isinstance(password_file, Path)
        assert password_file != _password_path(cfg)
        assert password_file.read_text(encoding="utf-8") == "bad-pw\n"
        raise CommandError(CommandResult(["kopia"], 1, "", "invalid password"))

    monkeypatch.setattr(installer_password.kopia_client, "repository_connect_filesystem", fail_connect)

    spec = kopia_password.PasswordSpec(literal="bad-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 1
    assert not _password_path(cfg).exists()
    assert not list(_password_path(cfg).parent.glob(".kopia.pw.verify.*.tmp"))
    err = capsys.readouterr().err
    assert "supplied kopia password did not connect to existing repo" in err
    assert "invalid password" in err


def test_install_password_existing_repo_missing_file_still_requires_ack(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _create_local_repo_sentinel(cfg)

    spec = kopia_password.PasswordSpec(literal="correct-pw")
    assert installer_password.install_password(cfg, spec) == 1
    assert not _password_path(cfg).exists()
    assert "--acknowledge-password-loss" in capsys.readouterr().err


def test_install_password_existing_repo_restores_missing_file_after_ack_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(tmp_path)
    _create_local_repo_sentinel(cfg)
    seen: list[Path] = []

    def fake_connect(**kwargs: object) -> None:
        password_file = kwargs["password_file"]
        assert isinstance(password_file, Path)
        seen.append(password_file)
        assert password_file.read_text(encoding="utf-8") == "correct-pw\n"

    monkeypatch.setattr(installer_password.kopia_client, "repository_connect_filesystem", fake_connect)

    spec = kopia_password.PasswordSpec(literal="correct-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 0
    assert seen and seen[0] != _password_path(cfg)
    assert _password_path(cfg).read_text(encoding="utf-8") == "correct-pw\n"


def test_install_password_existing_repo_rejects_wrong_file_without_repairing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _create_local_repo_sentinel(cfg)
    pw_path = _password_path(cfg)
    pw_path.parent.mkdir(parents=True)
    pw_path.write_text("stale-pw\n", encoding="utf-8")
    pw_path.chmod(0o600)

    def fake_connect(**kwargs: object) -> None:
        password_file = kwargs["password_file"]
        assert isinstance(password_file, Path)
        assert password_file != pw_path
        assert password_file.read_text(encoding="utf-8") == "correct-pw\n"

    monkeypatch.setattr(installer_password.kopia_client, "repository_connect_filesystem", fake_connect)

    spec = kopia_password.PasswordSpec(literal="correct-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 1
    assert pw_path.read_text(encoding="utf-8") == "stale-pw\n"
    assert "change-password" in capsys.readouterr().err


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
    spec = kopia_password.PasswordSpec(literal="install-pw", acknowledge_loss=True)
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


def test_change_password_requires_argv_acknowledgement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    assert installer_password.change_password(cfg, kopia_password.PasswordSpec(literal="rotated-pw")) == 1
    err = capsys.readouterr().err
    assert "--acknowledge-password-argv-exposure" in err


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
    spec = kopia_password.PasswordSpec(literal="rotated-pw", acknowledge_argv_exposure=True)
    assert installer_password.change_password(cfg, spec) == 0
    assert captured["value"] == "rotated-pw"
    assert captured["config"] is cfg
