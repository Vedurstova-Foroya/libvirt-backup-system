"""Tests for HOST_ID stamping + drift detection in preflight."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import preflight
from tests.unit._preflight_helpers import make_config


def test_stamp_host_id_creates_state_on_first_run(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    failures = preflight.stamp_host_id_on_first_run(cfg)
    assert failures == []
    state_path = preflight._host_id_state_path(cfg)
    assert state_path.read_text(encoding="utf-8").strip() == "alpha"


def test_stamp_host_id_detects_drift_against_existing_state(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    state_path = preflight._host_id_state_path(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("beta\n", encoding="utf-8")
    failures = preflight.stamp_host_id_on_first_run(cfg)
    assert any("HOST_ID drift detected" in failure for failure in failures)


def test_stamp_host_id_fills_empty_state(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    state_path = preflight._host_id_state_path(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("", encoding="utf-8")
    failures = preflight.stamp_host_id_on_first_run(cfg)
    assert failures == []
    assert state_path.read_text(encoding="utf-8").strip() == "alpha"


def test_stamp_host_id_skips_write_when_stamp_matches(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    state_path = preflight._host_id_state_path(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("alpha\n", encoding="utf-8")
    failures = preflight.stamp_host_id_on_first_run(cfg)
    assert failures == []


def test_stamp_host_id_handles_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(_self: Path) -> bool:
        raise OSError("ENOSPC")

    monkeypatch.setattr(Path, "exists", boom)
    failures = preflight.stamp_host_id_on_first_run(cfg)
    assert any("HOST_ID state check failed: ENOSPC" in failure for failure in failures)


def test_host_id_drift_failures_returns_empty_when_state_missing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    assert preflight.host_id_drift_failures(cfg) == []


def test_host_id_drift_failures_detects_drift(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    state_path = preflight._host_id_state_path(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("beta\n", encoding="utf-8")
    failures = preflight.host_id_drift_failures(cfg)
    assert failures and "HOST_ID drift detected" in failures[0]


def test_host_id_drift_failures_returns_empty_when_state_matches(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    state_path = preflight._host_id_state_path(cfg)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("alpha\n", encoding="utf-8")
    assert preflight.host_id_drift_failures(cfg) == []


def test_host_id_drift_failures_handles_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(_self: Path) -> bool:
        raise OSError("EACCES")

    monkeypatch.setattr(Path, "exists", boom)
    failures = preflight.host_id_drift_failures(cfg)
    assert failures == ["HOST_ID state check failed: EACCES"]
