from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_start import start
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    RUN_UNIT_NAME,
    STATUS_UNITS,
    TIMER_UNIT_NAME,
    status,
)


def test_status_returns_one_when_systemctl_unavailable(tmp_path: Path, capsys) -> None:
    assert status(str(tmp_path)) == 1
    assert "systemctl unavailable" in capsys.readouterr().err


def test_status_runs_systemctl_for_each_unit_and_returns_worst_rc(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    calls: list[list[str]] = []
    codes = iter([0, 3, 0])

    def fake_run(args, *, check):
        calls.append(args)
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 3
    assert [c[-1] for c in calls] == list(STATUS_UNITS)
    assert all(c[:3] == ["systemctl", "status", "--no-pager"] for c in calls)


def test_start_returns_one_when_systemctl_unavailable(tmp_path: Path, capsys) -> None:
    assert start(str(tmp_path)) == 1
    assert "systemctl unavailable" in capsys.readouterr().err


def test_start_requires_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "BACKUP_PATH is not configured" in err


def test_start_rejects_relative_config_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)

    assert start(str(tmp_path), config_path="relative.env") == 1

    err = capsys.readouterr().err
    assert "config_path must be an absolute path" in err


def test_start_rejects_relative_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config.parent.mkdir(parents=True)
    config.write_text("BACKUP_PATH=relative/backups\n", encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "BACKUP_PATH must be an absolute path" in err


def test_start_rejects_invalid_timer_calendar(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\nSYSTEMD_ON_CALENDAR=--help\n", encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "SYSTEMD_ON_CALENDAR must not start with '-'" in err


def test_start_installs_units_enables_and_starts_timer(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)

    assert start(str(tmp_path)) == 0

    assert calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", TIMER_UNIT_NAME],
    ]
    systemd_dir = tmp_path / "etc/systemd/system"
    assert (systemd_dir / RUN_UNIT_NAME).exists()
    assert (systemd_dir / CHECK_UNIT_NAME).exists()
    assert (systemd_dir / TIMER_UNIT_NAME).exists()
    out = capsys.readouterr().out
    assert "installed systemd units" in out
    assert "started systemd timer" in out


def test_start_returns_one_when_systemctl_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\n", encoding="utf-8")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 1, "", "boom")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)

    assert start(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "systemctl daemon-reload failed" in err
    assert "systemctl enable --now libvirt-backup-system.timer failed" in err
