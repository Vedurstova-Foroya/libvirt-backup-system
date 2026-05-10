from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import install
from libvirt_backup_system.shell import CommandResult


def _fake_config_factory(
    tmp_path: Path,
    *,
    backup_path: str | None = None,
    calendar: str | None = None,
    timeout_seconds: str | None = None,
) -> object:
    def fake_config(
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        values = dict(DEFAULTS)
        if backup_path is not None:
            values["BACKUP_PATH"] = backup_path
        if calendar is not None:
            values["SYSTEMD_ON_CALENDAR"] = calendar
        if timeout_seconds is not None:
            values["COMMAND_TIMEOUT_SECONDS"] = timeout_seconds
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    return fake_config


def _fake_systemd_root(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.installer.default_config_path",
        lambda root=None: tmp_path / "etc/config.env",
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)


def test_install_rejects_control_char_calendar(tmp_path: Path, monkeypatch, capsys) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(backup_dir), calendar="daily\nOnBootSec=1"),
    )

    assert install(str(tmp_path)) == 1

    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.timer").exists()
    assert "SYSTEMD_ON_CALENDAR must not contain control characters" in capsys.readouterr().err


def test_install_rejects_empty_calendar(tmp_path: Path, monkeypatch, capsys) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(backup_dir), calendar="  "),
    )

    assert install(str(tmp_path)) == 1

    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.timer").exists()
    assert "SYSTEMD_ON_CALENDAR must not be empty" in capsys.readouterr().err


def test_install_rejects_non_positive_command_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(backup_dir), timeout_seconds="0"),
    )
    assert install(str(tmp_path)) == 1
    assert "command timeout must be greater than 0" in capsys.readouterr().err


def test_install_validates_calendar_with_systemd_analyze(tmp_path: Path, monkeypatch) -> None:
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups"), calendar="daily"),
    )
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemd-analyze")
    analyze_calls: list[list[str]] = []
    systemctl_calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: analyze_calls.append(args) or CommandResult(args, 0, "", ""),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: systemctl_calls.append(args) or CommandResult(args, 0, "", ""),
    )

    assert install(None) == 0

    assert analyze_calls == [["systemd-analyze", "calendar", "daily"]]
    assert systemctl_calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "libvirt-backup-system.timer"],
    ]


def test_install_rejects_calendar_when_systemd_analyze_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups"), calendar="not-a-calendar"),
    )
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemd-analyze")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 1, "", "invalid calendar"),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError(f"unexpected command: {args}")),
    )

    assert install(None) == 1

    assert calls == [["systemd-analyze", "calendar", "not-a-calendar"]]
    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.timer").exists()
    err = capsys.readouterr().err
    assert "invalid systemd calendar" in err
    assert "invalid calendar" in err


def test_install_without_backup_path_disables_stale_units_and_reports_reload_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", _fake_config_factory(tmp_path, backup_path=""))
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        if args == ["systemctl", "daemon-reload"]:
            return CommandResult(args, 1, "", "reload failed")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.installer.run", fake_run)
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    timer_path = tmp_path / "etc/systemd/system/libvirt-backup-system.timer"
    service_path.parent.mkdir(parents=True)
    service_path.write_text("stale service\n", encoding="utf-8")
    timer_path.write_text("stale timer\n", encoding="utf-8")

    assert install(None) == 1

    assert calls == [
        ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
        ["systemctl", "stop", "libvirt-backup-system.service"],
        ["systemctl", "daemon-reload"],
    ]
    assert not service_path.exists()
    assert not timer_path.exists()
    err = capsys.readouterr().err
    assert "systemctl daemon-reload failed" in err
    assert "reload failed" in err
