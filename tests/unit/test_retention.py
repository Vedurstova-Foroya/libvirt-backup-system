from __future__ import annotations

from typing import Any

import pytest

from libvirt_backup_system import kopia_repo, retention
from libvirt_backup_system.config import Config


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
