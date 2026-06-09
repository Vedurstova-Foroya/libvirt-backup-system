from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from libvirt_backup_system import installer_password, kopia_password, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values["HOST_ID"] = "host-a"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    return cfg


def test_install_password_peer_repo_rejects_bad_supplied_password_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    peer_repo = cfg.path_value("BACKUP_PATH") / "host-b" / kopia_repo.REPO_DIR_NAME
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("repo\n", encoding="utf-8")

    def fail_connect(**kwargs: object) -> None:
        assert kwargs["repo_path"] == peer_repo
        assert kwargs["read_only"] is True
        password_file = kwargs["password_file"]
        assert isinstance(password_file, Path)
        assert password_file.read_text(encoding="utf-8") == "bad-pw\n"
        raise CommandError(CommandResult(["kopia"], 1, "", "invalid password"))

    monkeypatch.setattr(installer_password.kopia_client, "repository_connect_filesystem", fail_connect)

    spec = kopia_password.PasswordSpec(literal="bad-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 1
    assert not kopia_repo.password_file_path(cfg).exists()
    err = capsys.readouterr().err
    assert "did not connect to existing peer repo" in err
    assert "add-node" in err


def test_install_password_peer_repo_replaces_unused_generated_password_for_join(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(tmp_path)
    pw_path = kopia_repo.password_file_path(cfg)
    pw_path.parent.mkdir(parents=True)
    pw_path.write_text("generated-wrong-pw\n", encoding="utf-8")
    pw_path.chmod(0o600)
    peer_repo = cfg.path_value("BACKUP_PATH") / "host-b" / kopia_repo.REPO_DIR_NAME
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("repo\n", encoding="utf-8")
    seen: list[Path] = []

    def fake_connect(**kwargs: object) -> None:
        assert kwargs["repo_path"] == peer_repo
        assert kwargs["read_only"] is True
        password_file = kwargs["password_file"]
        assert isinstance(password_file, Path)
        seen.append(password_file)
        assert password_file.read_text(encoding="utf-8") == "correct-pw\n"

    monkeypatch.setattr(installer_password.kopia_client, "repository_connect_filesystem", fake_connect)

    spec = kopia_password.PasswordSpec(literal="correct-pw", acknowledge_loss=True)
    assert installer_password.install_password(cfg, spec) == 0

    assert seen and seen[0] != pw_path
    assert pw_path.read_text(encoding="utf-8") == "correct-pw\n"


def test_can_replace_unused_password_for_join_skips_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert installer_password._can_replace_unused_password_for_join(cfg, "pw") is False


def test_can_replace_unused_password_for_join_skips_when_no_peer_repos(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    assert installer_password._can_replace_unused_password_for_join(cfg, "pw") is False


def test_can_replace_unused_password_for_join_reports_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)

    def fail_discovery(_cfg: Config) -> NoReturn:
        raise kopia_repo.PeerDiscoveryError("cannot scan")

    monkeypatch.setattr(installer_password.kopia_repo, "discover_peer_repos", fail_discovery)

    assert installer_password._can_replace_unused_password_for_join(cfg, "pw") is False
    assert "kopia peer discovery failed during password validation" in capsys.readouterr().err


def test_peer_password_validation_skips_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""

    assert installer_password._supplied_password_connects_existing_peer_repos(cfg, "pw") is True


def test_peer_password_validation_reports_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)

    def fail_discovery(_cfg: Config) -> NoReturn:
        raise kopia_repo.PeerDiscoveryError("cannot scan")

    monkeypatch.setattr(installer_password.kopia_repo, "discover_peer_repos", fail_discovery)

    assert installer_password._supplied_password_connects_existing_peer_repos(cfg, "pw") is False
    assert "kopia peer discovery failed during password validation" in capsys.readouterr().err


def test_peer_password_validation_reports_temp_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    peer_repo = cfg.path_value("BACKUP_PATH") / "host-b" / kopia_repo.REPO_DIR_NAME
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("repo\n", encoding="utf-8")

    def fail_temp(_cfg: Config, _value: str) -> NoReturn:
        raise OSError("disk full")

    monkeypatch.setattr(installer_password.kopia_password, "temporary_password_file", fail_temp)

    assert installer_password._supplied_password_connects_existing_peer_repos(cfg, "pw") is False
    assert "temporary kopia peer password validation failed" in capsys.readouterr().err
