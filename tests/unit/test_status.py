from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.systemd_units import (
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    STATUS_UNITS,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
    status,
)


def test_status_returns_one_when_systemctl_unavailable(tmp_path: Path, capsys) -> None:
    assert status(str(tmp_path)) == 1
    assert "systemctl unavailable" in capsys.readouterr().err


def test_status_ignores_loaded_inactive_units(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    calls: list[list[str]] = []
    # Make at least one unit return rc=3 (the "loaded but inactive" case)
    # so _status_returncode is exercised; otherwise the loaded-inactive
    # downgrade branch goes uncovered when every status call already
    # returns 0.
    codes = iter([0, 3, *([0] * (len(STATUS_UNITS) - 2))])

    def fake_run(args, *, check, **kwargs):
        calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return type("R", (), {"returncode": 0, "stdout": "loaded\ninactive\n"})()
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 0
    status_calls = [c for c in calls if c[:3] == ["systemctl", "status", "--no-pager"]]
    assert [c[-1] for c in status_calls] == list(STATUS_UNITS)
    # STATUS_UNITS MUST cover the maintenance + verify pairs so operators
    # who run `lbs status` see the kopia housekeeping timers alongside the
    # backup timer.
    assert MAINTENANCE_TIMER_NAME in STATUS_UNITS
    assert MAINTENANCE_UNIT_NAME in STATUS_UNITS
    assert MAINTENANCE_FULL_TIMER_NAME in STATUS_UNITS
    assert MAINTENANCE_FULL_UNIT_NAME in STATUS_UNITS
    assert VERIFY_TIMER_NAME in STATUS_UNITS
    assert VERIFY_UNIT_NAME in STATUS_UNITS


def test_status_preserves_real_systemctl_failures(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    # First unit succeeds, second returns rc=4 (non-3, non-zero) so the
    # whole status() call returns 4 — the rest of the iteration is exhausted.
    codes = iter([0, 4, *([0] * (len(STATUS_UNITS) - 2))])

    def fake_run(args, *, check, **kwargs):
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 4


def test_status_preserves_failed_loaded_units(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    codes = iter([0, 3, *([0] * (len(STATUS_UNITS) - 2))])

    def fake_run(args, *, check, **kwargs):
        if args[:2] == ["systemctl", "show"]:
            return type("R", (), {"returncode": 0, "stdout": "loaded\nfailed\n"})()
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 3
