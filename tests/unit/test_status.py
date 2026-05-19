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


def test_status_ignores_loaded_inactive_units(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    calls: list[list[str]] = []
    codes = iter([0, 3, 0])

    def fake_run(args, *, check, **kwargs):
        calls.append(args)
        if args[:2] == ["systemctl", "show"]:
            return type("R", (), {"returncode": 0, "stdout": "loaded\ninactive\n"})()
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 0
    status_calls = [c for c in calls if c[:3] == ["systemctl", "status", "--no-pager"]]
    assert [c[-1] for c in status_calls] == list(STATUS_UNITS)


def test_status_preserves_real_systemctl_failures(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    codes = iter([0, 4, 0])

    def fake_run(args, *, check, **kwargs):
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 4


def test_status_preserves_failed_loaded_units(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    codes = iter([0, 3, 0])

    def fake_run(args, *, check, **kwargs):
        if args[:2] == ["systemctl", "show"]:
            return type("R", (), {"returncode": 0, "stdout": "loaded\nfailed\n"})()
        return type("R", (), {"returncode": next(codes)})()

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    assert status() == 3


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


def test_start_installs_units_enables_and_starts_timer_schedule(tmp_path: Path, monkeypatch, capsys) -> None:
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
        ["systemctl", "enable", TIMER_UNIT_NAME],
        ["systemctl", "start", TIMER_UNIT_NAME],
    ]
    systemd_dir = tmp_path / "etc/systemd/system"
    assert (systemd_dir / RUN_UNIT_NAME).exists()
    assert (systemd_dir / CHECK_UNIT_NAME).exists()
    assert (systemd_dir / TIMER_UNIT_NAME).exists()
    out = capsys.readouterr().out
    assert "installed systemd units" in out
    assert "started systemd timer schedule" in out
    assert "Persistent=true" not in (systemd_dir / TIMER_UNIT_NAME).read_text(encoding="utf-8")


def test_start_configures_timeout_before_calendar_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\nCOMMAND_TIMEOUT_SECONDS=7\n", encoding="utf-8")
    configured: list[str] = []

    def fake_configure(value: str) -> None:
        configured.append(value)

    def fake_render_timer(root: Path, calendar: str) -> str:
        assert configured == ["7"]
        return "[Timer]\n"

    monkeypatch.setattr("libvirt_backup_system.systemd_start.configure_default_timeout", fake_configure)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.render_unit_timer", fake_render_timer)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.run_systemctl", lambda root, commands: True)

    assert start(str(tmp_path)) == 0


def test_start_rejects_invalid_command_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\nCOMMAND_TIMEOUT_SECONDS=0\n", encoding="utf-8")

    assert start(str(tmp_path)) == 1
    assert "command timeout must be greater than 0" in capsys.readouterr().err


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
    assert "systemctl enable libvirt-backup-system.timer failed" in err
    assert "systemctl start libvirt-backup-system.timer failed" in err
