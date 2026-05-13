from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import install, uninstall
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

    fake_prefixed = lambda path, root: tmp_path / str(path).lstrip("/")  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", fake_prefixed)
    # systemd_units.run_systemctl uses its own ``prefixed`` import to resolve
    # the unit-file location for the "unit file absent => skip" check, so it
    # must be patched in the systemd_units namespace too.
    monkeypatch.setattr("libvirt_backup_system.systemd_units.prefixed", fake_prefixed)
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


def test_install_rejects_flag_shaped_calendar(tmp_path: Path, monkeypatch, capsys) -> None:
    # ``systemd-analyze calendar --help`` returns 0, so a value starting with
    # "-" would pass the rc check and render a broken unit file.
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(backup_dir), calendar="--help"),
    )

    assert install(str(tmp_path)) == 1
    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.timer").exists()
    assert "SYSTEMD_ON_CALENDAR must not start with '-'" in capsys.readouterr().err


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
    # systemd-analyze (render_unit_timer) and systemctl (run_systemctl) both
    # shell out via libvirt_backup_system.systemd_units.run after the systemctl
    # helpers moved into that module; discriminate by command name in the
    # shared fake to keep the original "what was called" assertion intact.
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups"), calendar="daily"),
    )
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: f"/bin/{binary}")
    analyze_calls: list[list[str]] = []
    systemctl_calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        (analyze_calls if args[:1] == ["systemd-analyze"] else systemctl_calls).append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)

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
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: f"/bin/{binary}")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if args[:1] == ["systemctl"]:
            raise AssertionError(f"unexpected systemctl command: {args}")
        calls.append(args)
        return CommandResult(args, 1, "", "invalid calendar")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)

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
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        if args == ["systemctl", "daemon-reload"]:
            return CommandResult(args, 1, "", "reload failed")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)
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


def test_install_does_not_delete_in_place_package_source(tmp_path: Path, monkeypatch, capsys) -> None:
    # Re-running ``install`` via the installed wrapper imports installer.py
    # from /opt/libvirt-backup-system/libvirt_backup_system — the very path
    # ``package_dst`` resolves to. The previous rmtree+copytree pair would
    # delete that source mid-execute and leave the host without an installed
    # package.
    opt_pkg = tmp_path / "opt/libvirt-backup-system/libvirt_backup_system"
    opt_pkg.mkdir(parents=True)
    sentinel = opt_pkg / "marker.py"
    sentinel.write_text("# preserved across in-place install\n", encoding="utf-8")
    fake_module_file = opt_pkg / "installer.py"
    fake_module_file.write_text("# stand-in for __file__\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.installer.__file__", str(fake_module_file))

    assert install(str(tmp_path)) == 0
    assert sentinel.exists(), "in-place install must not delete the live package source"
    assert "install reusing in-place package" in capsys.readouterr().out


def test_uninstall_is_idempotent_when_units_are_absent(tmp_path: Path, monkeypatch, capsys) -> None:
    # A second uninstall (or an uninstall on a host that never had the units)
    # must succeed silently: disable/stop of absent units are skipped rather
    # than passed to systemctl, which would return "Unit X does not exist".
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    # systemd_units.run_systemctl computes the unit-file path through its own
    # ``prefixed`` import, not installer's; without redirecting that too, the
    # absent-unit-file check stats the real /etc/systemd/system and falsely
    # passes on any developer host that has libvirt-backup-system installed.
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.prefixed", lambda path, root: tmp_path / str(path).lstrip("/")
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", _fake_config_factory(tmp_path))
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )

    assert uninstall(None) == 0
    assert calls == [["systemctl", "daemon-reload"]]
    out = capsys.readouterr().out
    assert "systemctl disable skipped because unit file is absent" in out
    assert "systemctl stop skipped because unit file is absent" in out


def test_install_replaces_existing_package_and_systemd_activation(tmp_path: Path, monkeypatch) -> None:
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups")),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.shutil.which",
        lambda binary: "/bin/systemctl" if binary == "systemctl" else None,
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    package = tmp_path / "opt/libvirt-backup-system/libvirt_backup_system"
    package.mkdir(parents=True)
    (package / "old.py").write_text("old\n", encoding="utf-8")
    assert install(None) == 0
    assert calls == [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "libvirt-backup-system.timer"]]


def test_install_systemctl_failure_emits_error_and_returns_nonzero(tmp_path: Path, monkeypatch, capsys) -> None:
    _fake_systemd_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups")),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.shutil.which",
        lambda binary: "/bin/systemctl" if binary == "systemctl" else None,
    )
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: CommandResult(args, 1, "", "boom"),
    )
    assert install(None) == 1
    err = capsys.readouterr().err
    assert "systemctl daemon-reload failed" in err
    assert "boom" in err
