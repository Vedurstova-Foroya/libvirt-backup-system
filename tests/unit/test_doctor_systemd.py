from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.doctor import _check_runtime_state, _expected_unit_text, doctor
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
)
from tests.unit.test_doctor import _fake_systemctl, _install_layout, _patch_check_pass


@pytest.mark.parametrize("unit_name", [RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME])
def test_doctor_reports_missing_unit(tmp_path: Path, monkeypatch, capsys, unit_name: str) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    (tmp_path / "etc/systemd/system" / unit_name).unlink()

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "systemd unit missing" in err
    assert unit_name in err


@pytest.mark.parametrize("unit_name", [RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME])
def test_doctor_reports_unit_drift(tmp_path: Path, monkeypatch, capsys, unit_name: str) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    unit_path = tmp_path / "etc/systemd/system" / unit_name
    unit_path.write_text(unit_path.read_text(encoding="utf-8") + "# drift\n", encoding="utf-8")

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "systemd unit out of date" in err
    assert unit_name in err


def test_doctor_reports_unrenderable_unit(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)

    def raising_render(*args: object, **kwargs: object) -> str:
        raise ValueError("synthetic render failure")

    monkeypatch.setattr("libvirt_backup_system.doctor.render_unit_service", raising_render)

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "doctor cannot render expected unit" in err
    assert "cannot validate" in err


def test_doctor_reports_timer_not_enabled(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch, enabled="disabled")

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "timer not enabled" in err
    assert "UnitFileState=disabled" in err


def test_doctor_reports_timer_not_active(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch, active="inactive")

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "timer not active" in err
    assert "ActiveState=inactive" in err


def test_doctor_reports_last_run_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch, result="exit-code")

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "last run failed" in err
    assert "Result=exit-code" in err


def test_doctor_passes_when_service_has_never_run(tmp_path: Path, monkeypatch, capsys) -> None:
    # Result="" is what ``systemctl show`` returns before the timer first fires
    # (or after a daemon-reload that resets the runtime state). Treat that as
    # "no failure yet", same as Result=success.
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch, result="")

    assert doctor(cfg) == 0
    assert "doctor passed" in capsys.readouterr().out


def test_doctor_skips_runtime_checks_when_systemctl_unavailable(tmp_path: Path, monkeypatch) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    monkeypatch.setattr("libvirt_backup_system.doctor.systemctl_available", lambda root: False)

    def fail_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise AssertionError(f"systemctl must not run when systemctl_available is False: {args}")

    monkeypatch.setattr("libvirt_backup_system.doctor.run", fail_run)
    assert _check_runtime_state(cfg.prefix) == []
    assert doctor(cfg) == 0


def test_doctor_treats_systemctl_show_failure_as_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch, enabled="enabled", active="active", result="success", returncode=4)

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    # Non-zero rc collapses each property to "" — enabled and active then mismatch
    # their expected values and the failure messages name the missing state as
    # "unknown" (the ``or 'unknown'`` fallback in the format string).
    assert "UnitFileState=unknown" in err
    assert "ActiveState=unknown" in err
    # Result="" is healthy by design, so no "last run failed" message.
    assert "last run failed" not in err


def test_expected_unit_text_dispatches_per_name(tmp_path: Path, monkeypatch) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    systemd_dir = tmp_path / "etc/systemd/system"
    assert _expected_unit_text(cfg, RUN_UNIT_NAME) == (systemd_dir / RUN_UNIT_NAME).read_text(encoding="utf-8")
    assert _expected_unit_text(cfg, CHECK_UNIT_NAME) == (systemd_dir / CHECK_UNIT_NAME).read_text(encoding="utf-8")
    assert _expected_unit_text(cfg, TIMER_UNIT_NAME) == (systemd_dir / TIMER_UNIT_NAME).read_text(encoding="utf-8")
