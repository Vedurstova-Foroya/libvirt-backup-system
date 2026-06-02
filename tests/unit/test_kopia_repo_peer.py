"""Peer-related edge-case tests for ``kopia_repo``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backup"),
            "HOST_ID": host_id,
        }
    )
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_password(config: Config) -> Path:
    path = kopia_repo.password_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("swordfish\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_ensure_peer_connected_rejects_unsafe_repo_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Lines 222-223: subpath_is_safe returns False for peer repo path."""
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    # Create a symlink under backup/ so that backup_path / host_id / kopia-repo
    # follows a symlink, making subpath_is_safe return False.
    host_id = "legit-host"
    evil_target = tmp_path / "evil"
    evil_target.mkdir()
    link = tmp_path / "backup" / host_id
    link.symlink_to(evil_target)

    def fail_connect(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("unsafe peer path must not reach kopia")

    monkeypatch.setattr(kopia_client, "repository_connect_filesystem", fail_connect)
    assert kopia_repo.ensure_peer_connected(cfg, host_id) is None
    assert "kopia peer repo path rejected" in capsys.readouterr().err


def test_discover_peer_repos_skips_non_directory_entries(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    backup = tmp_path / "backup"
    (backup / "stray-file").write_text("not a host", encoding="utf-8")
    repo = backup / "host-a" / "kopia-repo"
    repo.mkdir(parents=True)
    (repo / "kopia.repository.f").write_text("ok", encoding="utf-8")
    peers = kopia_repo.discover_peer_repos(cfg)
    assert [p.host_id for p in peers] == ["host-a"]
