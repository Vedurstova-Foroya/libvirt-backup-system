"""Unit coverage for the hidden ``kopia-passthrough`` subcommand.

These tests pin the behaviors operators actually rely on:
  * argv parsing forwards everything after ``--`` to kopia,
  * the default repo is the local host's repo,
  * ``--host-id`` routes through the peer-discovery helpers,
  * the kopia process exit code propagates back as the CLI exit code,
  * the constructed argv uses ``--config-file=...`` and the configured
    password file (via the kopia env builder).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import cli, kopia_repo
from libvirt_backup_system.cli import _kopia_passthrough_command, _resolve_passthrough_config_file, main
from libvirt_backup_system.cli_parser import build_parser
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backup"),
            "HOST_ID": host_id,
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
        }
    )
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    pw = kopia_repo.password_file_path(cfg)
    pw.parent.mkdir(parents=True, exist_ok=True)
    pw.write_text("swordfish\n", encoding="utf-8")
    pw.chmod(0o600)
    return cfg


class _CapturedRun:
    """Stand-in for ``subprocess.run`` that records argv and env."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], *, check: bool, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append({"cmd": list(cmd), "env": dict(env), "check": check})
        return subprocess.CompletedProcess(args=cmd, returncode=self.returncode)


def test_parser_captures_remainder_after_double_dash() -> None:
    parser = build_parser()
    args = parser.parse_args(["kopia-passthrough", "--", "snapshot", "list", "--all"])
    assert args.command == "kopia-passthrough"
    assert args.host_id is None
    # argparse.REMAINDER keeps the literal ``--`` so kopia's own flags don't
    # get re-parsed; the handler strips a single leading separator.
    assert args.kopia_args == ["--", "snapshot", "list", "--all"]


def test_parser_captures_host_id_and_remainder() -> None:
    parser = build_parser()
    args = parser.parse_args(["kopia-passthrough", "--host-id", "peer", "--", "snapshot", "list"])
    assert args.host_id == "peer"
    assert args.kopia_args == ["--", "snapshot", "list"]


def test_resolve_default_returns_local_config_file(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    resolved = _resolve_passthrough_config_file(cfg, None)
    assert resolved == kopia_repo.local_config_file(cfg)


def test_resolve_host_id_calls_ensure_peer_connected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    seen: dict[str, str] = {}

    def fake_ensure(config: Config, host_id: str) -> Path:
        seen["host_id"] = host_id
        return tmp_path / "peer.config"

    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", fake_ensure)
    resolved = _resolve_passthrough_config_file(cfg, "peer-host")
    assert resolved == tmp_path / "peer.config"
    assert seen == {"host_id": "peer-host"}


def test_resolve_host_id_returns_none_when_peer_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda config, host_id: None)
    assert _resolve_passthrough_config_file(cfg, "ghost") is None
    assert "kopia-passthrough peer repo not reachable" in capsys.readouterr().err


def test_kopia_passthrough_constructs_local_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    runner = _CapturedRun(returncode=0)
    monkeypatch.setattr("libvirt_backup_system.cli.subprocess.run", runner)
    args = build_parser().parse_args(["kopia-passthrough", "--", "snapshot", "list"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 0
    assert len(runner.calls) == 1
    cmd = runner.calls[0]["cmd"]
    assert cmd[0] == "kopia"
    expected_config = f"--config-file={kopia_repo.local_config_file(cfg)}"
    assert cmd[1] == expected_config
    # Operator's tail follows verbatim, with the leading ``--`` stripped.
    assert cmd[2:] == ["snapshot", "list"]
    env = runner.calls[0]["env"]
    # ``build_kopia_env`` reads KOPIA_PASSWORD from the password file path.
    assert env["KOPIA_PASSWORD"] == "swordfish"
    # ``check=False`` because the kopia exit code is the wrapper's exit code,
    # not a fatal error to raise.
    assert runner.calls[0]["check"] is False


def test_kopia_passthrough_propagates_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    runner = _CapturedRun(returncode=42)
    monkeypatch.setattr("libvirt_backup_system.cli.subprocess.run", runner)
    args = build_parser().parse_args(["kopia-passthrough", "--", "policy", "show", "--global"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 42


def test_kopia_passthrough_routes_host_id_to_peer_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    peer_cfg_file = tmp_path / "peer.config"
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda config, host_id: peer_cfg_file)
    runner = _CapturedRun(returncode=0)
    monkeypatch.setattr("libvirt_backup_system.cli.subprocess.run", runner)
    args = build_parser().parse_args(
        ["kopia-passthrough", "--host-id", "peer", "--", "snapshot", "list", "--tags=kind:meta"]
    )
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 0
    cmd = runner.calls[0]["cmd"]
    assert cmd[1] == f"--config-file={peer_cfg_file}"
    assert cmd[2:] == ["snapshot", "list", "--tags=kind:meta"]


def test_kopia_passthrough_rejects_empty_tail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _make_config(tmp_path)
    args = build_parser().parse_args(["kopia-passthrough"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 2
    assert "kopia-passthrough requires at least one kopia argument" in capsys.readouterr().err


def test_kopia_passthrough_rejects_bare_separator(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # ``-- `` with no following argv is the same shape as "no tail at all".
    cfg = _make_config(tmp_path)
    args = build_parser().parse_args(["kopia-passthrough", "--"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 2
    assert "kopia-passthrough requires at least one kopia argument" in capsys.readouterr().err


def test_kopia_passthrough_returns_one_when_peer_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda config, host_id: None)
    args = build_parser().parse_args(["kopia-passthrough", "--host-id", "ghost", "--", "snapshot", "list"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 1


def test_kopia_passthrough_returns_one_when_password_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    # Remove the password file to force the env-builder to surface its
    # CommandError as a clean operator-facing failure.
    kopia_repo.password_file_path(cfg).unlink()
    args = build_parser().parse_args(["kopia-passthrough", "--", "snapshot", "list"])
    rc = _kopia_passthrough_command(args, cfg)
    assert rc == 1
    assert "kopia-passthrough password unreadable" in capsys.readouterr().err


def test_main_dispatches_to_kopia_passthrough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(cli.Config, "load", lambda config_path=None, prefix=None: cfg)
    runner = _CapturedRun(returncode=7)
    monkeypatch.setattr("libvirt_backup_system.cli.subprocess.run", runner)
    rc = main(["kopia-passthrough", "--", "repository", "status"])
    assert rc == 7
    assert runner.calls and runner.calls[0]["cmd"][-2:] == ["repository", "status"]
