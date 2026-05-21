"""Tests for ``doctor._check_runtime_state`` and the ``_systemctl_value`` helper."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import doctor
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_units import RUN_UNIT_NAME, TIMER_UNIT_NAME
from tests.unit._doctor_helpers import stub_systemctl_values


def test_check_runtime_state_returns_empty_when_systemctl_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: False)
    assert doctor._check_runtime_state(tmp_path) == []


def test_check_runtime_state_timer_not_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "disabled",
            (TIMER_UNIT_NAME, "ActiveState"): "active",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "no",
            (RUN_UNIT_NAME, "Result"): "success",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "1",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "2",
        },
    )
    failures = doctor._check_runtime_state(Path("/"))
    assert any("timer not enabled" in failure for failure in failures)


def test_check_runtime_state_timer_not_active(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "enabled",
            (TIMER_UNIT_NAME, "ActiveState"): "inactive",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "no",
            (RUN_UNIT_NAME, "Result"): "success",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "1",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "2",
        },
    )
    failures = doctor._check_runtime_state(Path("/"))
    assert any("timer not active" in failure for failure in failures)


def test_check_runtime_state_needs_daemon_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "enabled",
            (TIMER_UNIT_NAME, "ActiveState"): "active",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "yes",
            (RUN_UNIT_NAME, "Result"): "success",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "1",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "2",
        },
    )
    failures = doctor._check_runtime_state(Path("/"))
    assert any("daemon-reload" in failure for failure in failures)


def test_check_runtime_state_timer_never_fired(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "enabled",
            (TIMER_UNIT_NAME, "ActiveState"): "active",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "no",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "0",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "0",
            (RUN_UNIT_NAME, "Result"): "success",
        },
    )
    failures = doctor._check_runtime_state(Path("/"))
    assert any("timer has not fired" in failure for failure in failures)


def test_check_runtime_state_timer_never_fired_but_next_elapse_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "enabled",
            (TIMER_UNIT_NAME, "ActiveState"): "active",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "no",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "0",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "next",
            (RUN_UNIT_NAME, "Result"): "success",
        },
    )
    assert doctor._check_runtime_state(Path("/")) == []


def test_check_runtime_state_last_run_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_systemctl_values(
        monkeypatch,
        {
            (TIMER_UNIT_NAME, "UnitFileState"): "enabled",
            (TIMER_UNIT_NAME, "ActiveState"): "active",
            (RUN_UNIT_NAME, "NeedDaemonReload"): "no",
            (RUN_UNIT_NAME, "Result"): "failure",
            (TIMER_UNIT_NAME, "LastTriggerUSec"): "1",
            (TIMER_UNIT_NAME, "NextElapseUSecRealtime"): "2",
        },
    )
    failures = doctor._check_runtime_state(Path("/"))
    assert any("last run failed" in failure for failure in failures)


def test_systemctl_value_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(args: list[str], **_kwargs: Any) -> CommandResult:
        return CommandResult(args, 1, "ignored", "")

    monkeypatch.setattr(doctor, "run", boom)
    assert doctor._systemctl_value("timer.service", "Result") == ""
