from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_repo, kopia_snapshots
from libvirt_backup_system import verify as verify_mod
from libvirt_backup_system.config import Config
from libvirt_backup_system.kopia_repo import PeerRepo
from libvirt_backup_system.shell import CommandError, CommandResult


def _ok_verify(**_: Any) -> None:
    return None


def _failing_verify(**_: Any) -> None:
    raise CommandError(CommandResult(args=["kopia", "snapshot", "verify"], returncode=2, stdout="", stderr="boom\n"))


def test_verify_local_repo_returns_zero_on_success(backup_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_verify(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", fake_verify)
    assert verify_mod.verify(backup_config) == 0
    # The local-repo path resolves config_file via ``kopia_repo.local_config_file``.
    assert calls[0]["config_file"] == kopia_repo.local_config_file(backup_config)
    assert calls[0]["password_file"] == kopia_repo.password_file_path(backup_config)
    assert calls[0]["cache_dir"] == kopia_repo.cache_dir(backup_config)


def test_verify_local_repo_returns_one_on_failure(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", _failing_verify)
    assert verify_mod.verify(backup_config) == 1
    err = capsys.readouterr().err
    # CommandError surfaces through the structured "kopia verify failed" event.
    assert "kopia verify failed" in err
    assert "boom" in err


def test_verify_peers_filters_by_include_hosts(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    peer_a = PeerRepo(host_id="host-a", repo_path=tmp_path / "a", config_file=tmp_path / "a.cfg")
    peer_b = PeerRepo(host_id="host-b", repo_path=tmp_path / "b", config_file=tmp_path / "b.cfg")
    peer_c = PeerRepo(host_id="host-c", repo_path=tmp_path / "c", config_file=tmp_path / "c.cfg")
    monkeypatch.setattr(kopia_repo, "iter_connected_peers", lambda _cfg: [peer_a, peer_b, peer_c])
    seen: list[Path] = []

    def fake_verify(**kwargs: Any) -> None:
        seen.append(kwargs["config_file"])

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", fake_verify)
    assert verify_mod.verify(backup_config, include_hosts=["host-a", "host-c"]) == 0
    # host-b is dropped because it is not in the include set.
    assert seen == [peer_a.config_file, peer_c.config_file]


def test_verify_peers_returns_one_when_any_peer_fails(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    peer_a = PeerRepo(host_id="host-a", repo_path=tmp_path / "a", config_file=tmp_path / "a.cfg")
    peer_b = PeerRepo(host_id="host-b", repo_path=tmp_path / "b", config_file=tmp_path / "b.cfg")
    monkeypatch.setattr(kopia_repo, "iter_connected_peers", lambda _cfg: [peer_a, peer_b])

    def selective(**kwargs: Any) -> None:
        if kwargs["config_file"] == peer_b.config_file:
            raise CommandError(CommandResult(args=["kopia"], returncode=3, stdout="", stderr="peer-b bad"))

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", selective)
    assert verify_mod.verify(backup_config, include_hosts=["host-a", "host-b"]) == 1


def test_verify_peers_returns_zero_when_no_peers_match(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # iter_connected_peers returns hosts that all get filtered out by
    # include_hosts. The result is vacuously ok=True so we return 0.
    peer = PeerRepo(host_id="host-a", repo_path=tmp_path / "a", config_file=tmp_path / "a.cfg")
    monkeypatch.setattr(kopia_repo, "iter_connected_peers", lambda _cfg: [peer])
    called: list[Any] = []
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", lambda **kw: called.append(kw))
    assert verify_mod.verify(backup_config, include_hosts=["other-host"]) == 0
    assert called == []


def test_verify_peers_returns_zero_when_iter_returns_empty(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(kopia_repo, "iter_connected_peers", lambda _cfg: [])
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", _ok_verify)
    assert verify_mod.verify(backup_config, include_hosts=[]) == 0


def test_verify_success_emits_info_event(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", _ok_verify)
    assert verify_mod.verify(backup_config) == 0
    assert "verify passed" in capsys.readouterr().out
