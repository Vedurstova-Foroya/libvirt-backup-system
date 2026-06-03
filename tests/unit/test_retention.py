from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo, retention
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult


def test_prune_old_months_is_noop_and_returns_zero(backup_config: Config) -> None:
    # The kopia engine owns chunk lifecycle, so the orchestrator step does
    # nothing. The signature is preserved only to keep ``cli._run_command``
    # compiling against the legacy entry point.
    assert retention.prune_old_months(backup_config) == 0


def test_prune_old_months_accepts_current_month_kwarg(backup_config: Config) -> None:
    # ``current_month`` exists for parity with the legacy signature; it must
    # be accepted but ignored.
    assert retention.prune_old_months(backup_config, current_month="2026-05") == 0


def test_apply_policy_delegates_to_ensure_local_repo_success(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[dict[str, Any]] = []

    def fake_ensure(config: Config, *, apply_global_policy: bool = False) -> int:
        captured.append({"config": config, "apply_global_policy": apply_global_policy})
        return 0

    monkeypatch.setattr(kopia_repo, "ensure_local_repo", fake_ensure)
    assert retention.apply_policy(backup_config) == 0
    assert len(captured) == 1
    # ``apply_policy`` must explicitly request the global policy refresh —
    # otherwise the operator's edits to KEEP_* would never land.
    assert captured[0]["apply_global_policy"] is True
    assert captured[0]["config"] is backup_config


def test_apply_policy_propagates_failure(backup_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_ensure(_config: Config, *, apply_global_policy: bool = False) -> int:
        _ = apply_global_policy
        return 1

    monkeypatch.setattr(kopia_repo, "ensure_local_repo", fake_ensure)
    assert retention.apply_policy(backup_config) == 1


def test_inspect_policy_returns_global_policy(backup_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    config_file = Path("/tmp/kopia.config")
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: config_file)

    def fake_show(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"retention": {"keepDaily": 14}}

    monkeypatch.setattr(kopia_client, "policy_show_global", fake_show)
    assert retention.inspect_policy(backup_config) == {"retention": {"keepDaily": 14}}
    assert captured["config_file"] == config_file
    assert captured["password_file"] == kopia_repo.password_file_path(backup_config)
    assert captured["cache_dir"] == kopia_repo.cache_dir(backup_config)


def test_inspect_policy_returns_none_when_connect_fails(backup_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: None)
    monkeypatch.setattr(kopia_client, "policy_show_global", lambda **_kw: pytest.fail("must not inspect"))
    assert retention.inspect_policy(backup_config) is None


def test_inspect_policy_logs_policy_show_failure(
    backup_config: Config, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: Path("/tmp/kopia.config"))

    def fail_show(**_kwargs: object) -> dict[str, object]:
        raise CommandError(CommandResult(["kopia", "policy", "show"], 2, "", "policy denied"))

    monkeypatch.setattr(kopia_client, "policy_show_global", fail_show)
    assert retention.inspect_policy(backup_config) is None
    assert "kopia policy inspection failed" in capsys.readouterr().err
