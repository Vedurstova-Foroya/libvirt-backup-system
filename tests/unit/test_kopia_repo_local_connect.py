from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update({"BACKUP_PATH": str(tmp_path / "backup"), "HOST_ID": "host-a"})
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_password(config: Config) -> None:
    path = kopia_repo.password_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("swordfish\n", encoding="utf-8")
    path.chmod(0o600)


class _RunRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self, args: list[str], *, check: bool = True, env: Mapping[str, str] | None = None, **_: Any
    ) -> CommandResult:
        self.calls.append(args)
        return CommandResult(args, 0, "", "")


def test_ensure_local_connected_reconnects_existing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    repo_path = kopia_repo.local_repo_path(cfg)
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "kopia.repository.f").write_text("sentinel", encoding="utf-8")

    recorder = _RunRecorder()
    monkeypatch.setattr(kopia_client, "run", recorder)
    monkeypatch.setattr(kopia_client, "run_streamed", recorder)

    assert kopia_repo.ensure_local_connected(cfg) == kopia_repo.local_config_file(cfg)
    assert recorder.calls
    assert recorder.calls[0][recorder.calls[0].index("repository") + 1] == "connect"
    assert str(repo_path) in recorder.calls[0]
    assert "--readonly" not in recorder.calls[0]


def test_ensure_local_connected_refuses_missing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    monkeypatch.setattr(kopia_client, "run", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    monkeypatch.setattr(kopia_client, "run_streamed", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    assert kopia_repo.ensure_local_connected(cfg) is None


def test_ensure_local_connected_returns_none_when_password_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lines 149-150: password file missing logs and returns None."""
    cfg = _make_config(tmp_path)
    # Do NOT write a password file
    monkeypatch.setattr(kopia_client, "run", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    monkeypatch.setattr(kopia_client, "run_streamed", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    assert kopia_repo.ensure_local_connected(cfg) is None
    assert "kopia password file missing" in capsys.readouterr().err


def test_ensure_local_connected_returns_none_when_repo_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lines 153-155: invalid repo path logs and returns None."""
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "outside")
    monkeypatch.setattr(kopia_client, "run", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    monkeypatch.setattr(kopia_client, "run_streamed", lambda *_a, **_kw: pytest.fail("must not invoke kopia"))
    assert kopia_repo.ensure_local_connected(cfg) is None
    assert "kopia repo path rejected" in capsys.readouterr().err


def test_ensure_local_connected_returns_none_on_connect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lines 169-171: CommandError during connect logs and returns None."""
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    repo_path = kopia_repo.local_repo_path(cfg)
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "kopia.repository.f").write_text("sentinel", encoding="utf-8")

    def boom(args: list[str], **_: Any) -> CommandResult:
        raise CommandError(CommandResult(args, 3, "", "connect denied"))

    monkeypatch.setattr(kopia_client, "run", boom)
    monkeypatch.setattr(kopia_client, "run_streamed", boom)
    assert kopia_repo.ensure_local_connected(cfg) is None
    assert "kopia local repo connect failed" in capsys.readouterr().err
