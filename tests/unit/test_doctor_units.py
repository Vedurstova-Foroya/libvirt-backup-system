"""Tests for ``doctor._check_units``."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import doctor
from libvirt_backup_system.systemd_units import CHECK_UNIT_NAME, RUN_UNIT_NAME, TIMER_UNIT_NAME
from tests.unit._doctor_helpers import make_config, write_unit


def test_check_units_stale_units_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    write_unit(cfg, RUN_UNIT_NAME)
    failures = doctor._check_units(cfg)
    assert any("systemd units present but BACKUP_PATH is empty" in failure for failure in failures)


def test_check_units_no_failures_when_backup_path_empty_and_no_units(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    assert doctor._check_units(cfg) == []


def test_check_units_missing_unit_file(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    failures = doctor._check_units(cfg)
    assert any("systemd unit missing" in failure for failure in failures)


def test_check_units_render_failure_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_unit(cfg, RUN_UNIT_NAME)
    write_unit(cfg, CHECK_UNIT_NAME)
    write_unit(cfg, TIMER_UNIT_NAME)
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: None)
    failures = doctor._check_units(cfg)
    assert any("cannot validate" in failure for failure in failures)


def test_check_units_out_of_date(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_unit(cfg, RUN_UNIT_NAME, content="actual content")
    write_unit(cfg, CHECK_UNIT_NAME, content="actual content")
    write_unit(cfg, TIMER_UNIT_NAME, content="actual content")
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: "expected content")
    failures = doctor._check_units(cfg)
    assert any("systemd unit out of date" in failure for failure in failures)


def test_check_units_matches_expected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_unit(cfg, RUN_UNIT_NAME, content="x")
    write_unit(cfg, CHECK_UNIT_NAME, content="x")
    write_unit(cfg, TIMER_UNIT_NAME, content="x")
    monkeypatch.setattr(doctor, "_expected_unit_text", lambda _cfg, _name: "x")
    assert doctor._check_units(cfg) == []
