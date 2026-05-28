from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult


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


class _RunRecorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        **_: Any,
    ) -> CommandResult:
        self.calls.append(args)
        return CommandResult(args, 0, "", "")


def test_local_repo_path_falls_back_to_convention(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = ""
    assert kopia_repo.local_repo_path(cfg) == Path(tmp_path / "backup" / "host-a" / "kopia-repo")


def test_local_repo_path_honors_override(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "backup" / "host-a" / "kopia-repo")
    assert kopia_repo.local_repo_path(cfg) == tmp_path / "backup" / "host-a" / "kopia-repo"


def test_local_repo_path_rejects_non_discoverable_override(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "backup" / "custom-repo")
    with pytest.raises(ValueError, match="must use BACKUP_PATH/HOST_ID/kopia-repo"):
        kopia_repo.local_repo_path(cfg)


def test_local_repo_path_rejects_override_outside_backup_path(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "custom-repo")
    with pytest.raises(ValueError, match="must stay within BACKUP_PATH"):
        kopia_repo.local_repo_path(cfg)


def test_local_repo_path_rejects_relative_override(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = "relative/repo"
    with pytest.raises(ValueError, match="absolute path"):
        kopia_repo.local_repo_path(cfg)


def test_local_repo_exists_returns_false_for_rejected_override(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "outside")
    assert kopia_repo.local_repo_exists(cfg) is False


def test_ensure_local_repo_creates_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    recorder = _RunRecorder()
    monkeypatch.setattr(kopia_client, "run", recorder)
    monkeypatch.setattr(kopia_client, "run_streamed", recorder)

    assert kopia_repo.ensure_local_repo(cfg) == 0

    actions = [args[args.index("repository") + 1] for args in recorder.calls if "repository" in args]
    assert "create" in actions
    policy_calls = [args for args in recorder.calls if "policy" in args]
    assert policy_calls, "global policy must be applied on first install"
    set_owner_calls = [args for args in recorder.calls if "set" in args and "maintenance" in args]
    assert not set_owner_calls, "local repo setup must not claim maintenance ownership"


def test_ensure_local_repo_connects_when_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    repo_path = kopia_repo.local_repo_path(cfg)
    repo_path.mkdir(parents=True, exist_ok=True)
    (repo_path / "kopia.repository.f").write_text("sentinel", encoding="utf-8")

    recorder = _RunRecorder()
    monkeypatch.setattr(kopia_client, "run", recorder)
    monkeypatch.setattr(kopia_client, "run_streamed", recorder)
    assert kopia_repo.ensure_local_repo(cfg) == 0
    actions = [args[args.index("repository") + 1] for args in recorder.calls if "repository" in args]
    assert "connect" in actions
    assert "create" not in actions


def test_ensure_local_repo_fails_if_password_missing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    # Do not write a password file
    assert kopia_repo.ensure_local_repo(cfg) == 1
    assert "kopia password file missing" in capsys.readouterr().err


def test_ensure_local_repo_surfaces_kopia_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)

    def boom(args: list[str], **_: Any) -> CommandResult:
        raise CommandError(CommandResult(args, 2, "", "no repo here"))

    monkeypatch.setattr(kopia_client, "run", boom)
    monkeypatch.setattr(kopia_client, "run_streamed", boom)
    assert kopia_repo.ensure_local_repo(cfg) == 1


def test_ensure_local_repo_rejects_override_outside_backup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["KOPIA_REPO_PATH"] = str(tmp_path / "outside")
    _write_password(cfg)

    def fail_run(*_args: Any, **_kwargs: Any) -> CommandResult:
        pytest.fail("kopia must not be invoked for an unsafe repo path")

    monkeypatch.setattr(kopia_client, "run", fail_run)
    monkeypatch.setattr(kopia_client, "run_streamed", fail_run)
    assert kopia_repo.ensure_local_repo(cfg) == 1
    assert "kopia repo path rejected" in capsys.readouterr().err


def test_discover_peer_repos_lists_present_repos(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    for host in ("host-a", "host-b"):
        repo_dir = tmp_path / "backup" / host / "kopia-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "kopia.repository.f").write_text("ok", encoding="utf-8")
    (tmp_path / "backup" / "host-c").mkdir()  # no kopia-repo subdir
    peers = kopia_repo.discover_peer_repos(cfg)
    host_ids = sorted(peer.host_id for peer in peers)
    assert host_ids == ["host-a", "host-b"]


def test_discover_peer_repos_returns_empty_when_no_backup_path(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = str(tmp_path / "does-not-exist")
    assert kopia_repo.discover_peer_repos(cfg) == []


def test_ensure_peer_connected_returns_none_when_repo_missing(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    assert kopia_repo.ensure_peer_connected(cfg, "ghost-host") is None


def test_ensure_peer_connected_runs_readonly_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    peer_repo = tmp_path / "backup" / "host-b" / "kopia-repo"
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("ok", encoding="utf-8")
    recorder = _RunRecorder()
    monkeypatch.setattr(kopia_client, "run", recorder)
    monkeypatch.setattr(kopia_client, "run_streamed", recorder)
    config_file = kopia_repo.ensure_peer_connected(cfg, "host-b")
    assert config_file is not None
    args = recorder.calls[0]
    assert "--readonly" in args
    assert str(peer_repo) in args


def test_ensure_peer_connected_returns_none_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    peer_repo = tmp_path / "backup" / "host-b" / "kopia-repo"
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("ok", encoding="utf-8")

    def boom(args: list[str], **_: Any) -> CommandResult:
        raise CommandError(CommandResult(args, 5, "", "denied"))

    monkeypatch.setattr(kopia_client, "run", boom)
    monkeypatch.setattr(kopia_client, "run_streamed", boom)
    assert kopia_repo.ensure_peer_connected(cfg, "host-b") is None


def test_iter_connected_peers_skips_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    good = tmp_path / "backup" / "host-good" / "kopia-repo"
    good.mkdir(parents=True)
    (good / "kopia.repository.f").write_text("ok", encoding="utf-8")
    bad = tmp_path / "backup" / "host-bad" / "kopia-repo"
    bad.mkdir(parents=True)
    (bad / "kopia.repository.f").write_text("ok", encoding="utf-8")

    def selective(args: list[str], *, check: bool = True, **_: Any) -> CommandResult:
        if "host-bad" in " ".join(args):
            raise CommandError(CommandResult(args, 2, "", "fail"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(kopia_client, "run", selective)
    monkeypatch.setattr(kopia_client, "run_streamed", selective)
    peers = kopia_repo.iter_connected_peers(cfg)
    assert [peer.host_id for peer in peers] == ["host-good"]


def test_local_repo_exists_detects_sentinel(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    repo = kopia_repo.local_repo_path(cfg)
    repo.mkdir(parents=True, exist_ok=True)
    assert kopia_repo.local_repo_exists(cfg) is False
    (repo / "kopia.repository.f").write_text("", encoding="utf-8")
    assert kopia_repo.local_repo_exists(cfg) is True


def test_ensure_local_repo_fails_when_policy_set_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    repo = kopia_repo.local_repo_path(cfg)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "kopia.repository.f").write_text("ok", encoding="utf-8")

    def fake_run(args: list[str], *, check: bool = True, **_: Any) -> CommandResult:
        if "policy" in args:
            raise CommandError(CommandResult(args, 4, "", "policy-fail"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(kopia_client, "run", fake_run)
    monkeypatch.setattr(kopia_client, "run_streamed", fake_run)
    assert kopia_repo.ensure_local_repo(cfg) == 1


def test_apply_global_policy_skips_empty_int_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    cfg.values["KEEP_LATEST"] = ""
    recorder = _RunRecorder()
    monkeypatch.setattr(kopia_client, "run", recorder)
    monkeypatch.setattr(kopia_client, "run_streamed", recorder)
    assert kopia_repo.ensure_local_repo(cfg) == 0
    policy_args = next(args for args in recorder.calls if "policy" in args)
    assert "--keep-latest" not in policy_args


def test_discover_peer_repos_logs_when_iterdir_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)

    def boom(_self: Path) -> Any:
        raise OSError("nfs hiccup")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert kopia_repo.discover_peer_repos(cfg) == []
    assert "kopia peer discovery failed" in capsys.readouterr().err


def test_iter_connected_peers_returns_empty_with_no_peers(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    assert kopia_repo.iter_connected_peers(cfg) == []


def test_discover_peer_repos_skips_non_directory_entries(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    backup = tmp_path / "backup"
    (backup / "stray-file").write_text("not a host", encoding="utf-8")
    repo = backup / "host-a" / "kopia-repo"
    repo.mkdir(parents=True)
    (repo / "kopia.repository.f").write_text("ok", encoding="utf-8")
    peers = kopia_repo.discover_peer_repos(cfg)
    assert [p.host_id for p in peers] == ["host-a"]
