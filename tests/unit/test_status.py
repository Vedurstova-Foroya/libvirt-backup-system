from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.systemd_units import STATUS_UNITS, status


def test_status_returns_one_when_systemctl_unavailable(tmp_path: Path, capsys) -> None:
    assert status(str(tmp_path)) == 1
    assert "systemctl unavailable" in capsys.readouterr().err


def test_status_runs_systemctl_for_each_unit_and_returns_worst_rc(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    calls: list[list[str]] = []
    codes = iter([0, 3])

    def fake_run(args, *, check):
        calls.append(args)
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 3
    assert [c[-1] for c in calls] == list(STATUS_UNITS)
    assert all(c[:3] == ["systemctl", "status", "--no-pager"] for c in calls)
