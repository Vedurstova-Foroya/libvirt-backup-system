"""Edge-case tests for ``installer_password`` — repo-path validation and temp-file errors."""

from __future__ import annotations

import contextlib
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


def _create_local_repo_sentinel(cfg: Config) -> Path:
    repo_path = kopia_repo.local_repo_path(cfg)
    repo_path.mkdir(parents=True)
    (repo_path / "kopia.repository.f").write_text("repo\n", encoding="utf-8")
    return repo_path


def test_install_password_existing_repo_rejects_invalid_repo_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ValueError from ``local_repo_path`` is caught and surfaced as an error event."""
    cfg = _make_config(tmp_path)
    _create_local_repo_sentinel(cfg)

    # local_repo_exists must still return True so install_password reaches the
    # _supplied_password_connects_existing_repo branch; only then does the
    # ValueError from local_repo_path inside that helper get exercised.
    monkeypatch.setattr(installer_password.kopia_repo, "local_repo_exists", lambda _cfg: True)

    def boom_local_repo_path(_cfg: object) -> None:
        raise ValueError("KOPIA_REPO_PATH must be an absolute path")

    monkeypatch.setattr(installer_password.kopia_repo, "local_repo_path", boom_local_repo_path)

    spec = kopia_password.PasswordSpec(literal="good-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 1
    assert not _password_path(cfg).exists()
    err = capsys.readouterr().err
    assert "kopia repo path rejected" in err
    assert "KOPIA_REPO_PATH must be an absolute path" in err


def test_install_password_existing_repo_reports_temp_file_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OSError from ``temporary_password_file`` is caught and surfaced as an error event."""
    cfg = _make_config(tmp_path)
    _create_local_repo_sentinel(cfg)

    @contextlib.contextmanager
    def boom_temp_file(_cfg: object, _value: str):
        raise OSError("disk full")
        yield  # pragma: no cover - never reached; satisfies generator requirement

    monkeypatch.setattr(installer_password.kopia_password, "temporary_password_file", boom_temp_file)

    spec = kopia_password.PasswordSpec(literal="good-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 1
    assert not _password_path(cfg).exists()
    err = capsys.readouterr().err
    assert "temporary kopia password validation failed" in err
    assert "disk full" in err
