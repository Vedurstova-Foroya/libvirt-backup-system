from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_repo, kopia_snapshots
from libvirt_backup_system import verify as verify_mod
from libvirt_backup_system.config import Config
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


def test_verify_include_hosts_verifies_local_repo_and_selected_peers(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    peer_a_config = tmp_path / "a.cfg"
    peer_c_config = tmp_path / "c.cfg"
    peer_configs = {"host-a": peer_a_config, "host-c": peer_c_config}
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, host_id: peer_configs.get(host_id))
    seen: list[Path] = []

    def fake_verify(**kwargs: Any) -> None:
        seen.append(kwargs["config_file"])

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", fake_verify)
    assert verify_mod.verify(backup_config, include_hosts=["host-a", "host-c"]) == 0
    assert seen == [kopia_repo.local_config_file(backup_config), peer_a_config, peer_c_config]


def test_verify_peers_returns_one_when_any_peer_fails(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    peer_a_config = tmp_path / "a.cfg"
    peer_b_config = tmp_path / "b.cfg"
    peer_configs = {"host-a": peer_a_config, "host-b": peer_b_config}
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, host_id: peer_configs.get(host_id))

    def selective(**kwargs: Any) -> None:
        if kwargs["config_file"] == peer_b_config:
            raise CommandError(CommandResult(args=["kopia"], returncode=3, stdout="", stderr="peer-b bad"))

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", selective)
    assert verify_mod.verify(backup_config, include_hosts=["host-a", "host-b"]) == 1


def test_verify_include_hosts_returns_one_when_local_repo_fails(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    peer_config = tmp_path / "a.cfg"
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: peer_config)

    def selective(**kwargs: Any) -> None:
        if kwargs["config_file"] == kopia_repo.local_config_file(backup_config):
            raise CommandError(CommandResult(args=["kopia"], returncode=3, stdout="", stderr="local bad"))

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", selective)
    assert verify_mod.verify(backup_config, include_hosts=["host-a"]) == 1


def test_verify_include_hosts_returns_one_when_requested_peer_unavailable(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called: list[Any] = []
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: None)
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", lambda **kw: called.append(kw))
    assert verify_mod.verify(backup_config, include_hosts=["other-host"]) == 1
    assert [call["config_file"] for call in called] == [kopia_repo.local_config_file(backup_config)]
    err = capsys.readouterr().err
    assert "requested peer repo unavailable" in err
    assert "other-host" in err


def test_verify_peers_returns_zero_when_iter_returns_empty(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[Any] = []

    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: None)
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", lambda **kw: called.append(kw))
    assert verify_mod.verify(backup_config, include_hosts=[]) == 0
    assert [call["config_file"] for call in called] == [kopia_repo.local_config_file(backup_config)]


def test_verify_success_emits_info_event(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", _ok_verify)
    assert verify_mod.verify(backup_config) == 0
    assert "verify passed" in capsys.readouterr().out
