"""Tests for ``doctor._check_units``."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import doctor
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)
from tests.unit._doctor_helpers import make_config, write_unit


def _write_all_units(cfg: doctor.Config, content: str = "x") -> None:
    for name in doctor.DOCTOR_UNIT_NAMES:
        write_unit(cfg, name, content=content)


def test_check_units_stale_units_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    write_unit(cfg, RUN_UNIT_NAME)
    failures = doctor._check_units(cfg)
    assert any("systemd units present but BACKUP_PATH is empty" in failure for failure in failures)


def test_check_units_stale_maintenance_when_backup_path_empty(tmp_path: Path) -> None:
    # A user who clears BACKUP_PATH after a successful install MUST be told
    # about leftover maintenance/verify unit files, not just the legacy
    # backup pair.
    cfg = make_config(tmp_path, with_backup_path=False)
    write_unit(cfg, MAINTENANCE_UNIT_NAME)
    failures = doctor._check_units(cfg)
    assert any("systemd units present but BACKUP_PATH is empty" in failure for failure in failures)
    assert any(MAINTENANCE_UNIT_NAME in failure for failure in failures)


def test_check_units_no_failures_when_backup_path_empty_and_no_units(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    assert doctor._check_units(cfg) == []


def test_check_units_missing_unit_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    failures = doctor._check_units(cfg)
    assert any("systemd unit missing" in failure for failure in failures)
    # Maintenance/verify unit names MUST appear in the missing-list so an
    # operator can copy-paste the path into a `cp` or `ls`.
    assert any(MAINTENANCE_UNIT_NAME in failure for failure in failures)
    assert any(MAINTENANCE_FULL_UNIT_NAME in failure for failure in failures)
    assert any(MAINTENANCE_TIMER_NAME in failure for failure in failures)
    assert any(MAINTENANCE_FULL_TIMER_NAME in failure for failure in failures)
    assert any(VERIFY_UNIT_NAME in failure for failure in failures)
    assert any(VERIFY_TIMER_NAME in failure for failure in failures)


def test_check_units_render_failure_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _write_all_units(cfg)
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: None)
    failures = doctor._check_units(cfg)
    assert any("cannot validate" in failure for failure in failures)


def test_check_units_out_of_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _write_all_units(cfg, content="actual content")
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: "expected content")
    failures = doctor._check_units(cfg)
    assert any("systemd unit out of date" in failure for failure in failures)


def test_check_units_matches_expected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _write_all_units(cfg, content="x")
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: "x")
    assert doctor._check_units(cfg) == []


def test_check_units_subset_present_subset_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Render the maintenance unit but leave verify on the floor — the
    # missing-list MUST single out the verify pair so doctor's output
    # narrows the operator's investigation.
    cfg = make_config(tmp_path)
    write_unit(cfg, RUN_UNIT_NAME, content="x")
    write_unit(cfg, CHECK_UNIT_NAME, content="x")
    write_unit(cfg, TIMER_UNIT_NAME, content="x")
    write_unit(cfg, MAINTENANCE_UNIT_NAME, content="x")
    write_unit(cfg, MAINTENANCE_TIMER_NAME, content="x")
    write_unit(cfg, MAINTENANCE_FULL_UNIT_NAME, content="x")
    write_unit(cfg, MAINTENANCE_FULL_TIMER_NAME, content="x")
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: "x")
    failures = doctor._check_units(cfg)
    assert any(VERIFY_UNIT_NAME in failure and "missing" in failure for failure in failures)
    assert any(VERIFY_TIMER_NAME in failure and "missing" in failure for failure in failures)
