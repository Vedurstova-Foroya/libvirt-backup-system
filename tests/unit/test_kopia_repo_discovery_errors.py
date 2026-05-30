from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import kopia_repo
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    cfg.values["HOST_ID"] = "host-a"
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_repo(root: Path, host_id: str) -> None:
    repo = root / host_id / "kopia-repo"
    repo.mkdir(parents=True)
    (repo / "kopia.repository.f").write_text("ok\n", encoding="utf-8")


def test_discover_peer_repos_skips_when_host_entry_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    _write_repo(tmp_path / "backup", "host-c")
    host_dir = tmp_path / "backup" / "host-b"
    host_dir.mkdir()
    original_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == host_dir:
            raise OSError("host entry degraded")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    peers = kopia_repo.discover_peer_repos(cfg)
    assert [peer.host_id for peer in peers] == ["host-c"]
    assert "kopia peer host dir skipped" in capsys.readouterr().err


def test_discover_peer_repos_skips_when_repo_sentinel_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    _write_repo(tmp_path / "backup", "host-c")
    sentinel = tmp_path / "backup" / "host-b" / "kopia-repo" / "kopia.repository.f"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("ok\n", encoding="utf-8")
    original_stat = Path.stat

    def fake_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == sentinel:
            raise OSError("sentinel degraded")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    peers = kopia_repo.discover_peer_repos(cfg)
    assert [peer.host_id for peer in peers] == ["host-c"]
    assert "kopia peer repo sentinel skipped" in capsys.readouterr().err
