from __future__ import annotations

import contextlib
import runpy
import shutil
from pathlib import Path

import pytest

from libvirt_backup_system import __version__
from libvirt_backup_system.cli import build_parser, main
from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import UNIT_SERVICE, UNIT_TIMER, install, uninstall
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM


def test_install_and_uninstall_preserves_and_purges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    assert install(str(tmp_path)) == 0
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    assert bin_path.exists()
    assert config_path.exists()
    assert "BACKUP_PATH=\n" in config_path.read_text(encoding="utf-8")
    assert not service_path.exists()

    (tmp_path / "var/lib/libvirt-backup-system").mkdir(parents=True)
    (tmp_path / "var/log/libvirt-backup-system").mkdir(parents=True)
    assert uninstall(str(tmp_path)) == 0
    assert not bin_path.exists()
    assert config_path.exists()

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "BACKUP_PATH=",
            f"BACKUP_PATH={tmp_path / 'backups'}",
        ),
        encoding="utf-8",
    )
    assert install(str(tmp_path)) == 0
    service_text = service_path.read_text(encoding="utf-8")
    assert "ExecStart=/usr/local/bin/libvirt-backup-system run" in service_text
    assert f"RequiresMountsFor={tmp_path / 'backups'}" in service_text
    timer_path = tmp_path / "etc/systemd/system/libvirt-backup-system.timer"
    assert "OnCalendar=" in timer_path.read_text(encoding="utf-8")

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"BACKUP_PATH={tmp_path / 'backups'}",
            "BACKUP_PATH=",
        ),
        encoding="utf-8",
    )
    assert install(str(tmp_path)) == 0
    assert not service_path.exists()
    assert not timer_path.exists()

    assert uninstall(str(tmp_path), purge_config=True, purge_state=True, purge_logs=True, purge_backups=True) == 0
    assert not config_path.exists()


def test_install_replaces_existing_package_and_systemd_activation(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_config(prefix: str | None = None) -> Config:
        values = dict(DEFAULTS)
        values["BACKUP_PATH"] = str(tmp_path / "backups")
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.installer.default_config_path", lambda root=None: tmp_path / "etc/config.env"
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", fake_config)
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    package = tmp_path / "opt/libvirt-backup-system/libvirt_backup_system"
    package.mkdir(parents=True)
    (package / "old.py").write_text("old\n", encoding="utf-8")
    assert install(None) == 0
    assert calls == [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "libvirt-backup-system.timer"]]


def test_install_reports_stale_systemd_unit_removal_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    service_path.parent.mkdir(parents=True)
    service_path.write_text("stale\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == service_path:
            raise PermissionError("no perms")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert install(str(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "failed to remove stale systemd unit" in err
    assert "no perms" in err


def test_install_systemctl_failure_emits_error_and_returns_nonzero(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    original_exists = Path.exists

    def fake_config(prefix: str | None = None) -> Config:
        values = dict(DEFAULTS)
        values["BACKUP_PATH"] = str(tmp_path / "backups")
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.installer.default_config_path", lambda root=None: tmp_path / "etc/config.env"
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", fake_config)
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: CommandResult(args, 1, "", "boom"),
    )
    assert install(None) == 1
    err = capsys.readouterr().err
    assert "systemctl daemon-reload failed" in err
    assert "boom" in err


def test_uninstall_systemd_activation_and_missing_files(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_config(prefix: str | None = None) -> Config:
        return Config(values=dict(DEFAULTS), path=tmp_path / "etc/config.env", prefix=tmp_path)

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", fake_config)
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    assert uninstall(None) == 0
    assert calls[0] == ["systemctl", "disable", "--now", "libvirt-backup-system.timer"]
    assert calls[-1] == ["systemctl", "daemon-reload"]


def test_cli_commands(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.cli.install", lambda prefix: 11)
    assert main(["--prefix", str(tmp_path), "install"]) == 11

    monkeypatch.setattr("libvirt_backup_system.cli.uninstall", lambda prefix, **kwargs: 12)
    assert main(["--prefix", str(tmp_path), "uninstall", "--purge-config"]) == 12

    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: "cfg")
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    assert main(["check"]) == 0
    assert main(["preflight"]) == 0

    @contextlib.contextmanager
    def fake_lock(config: object):
        yield Path("/tmp/fake.lock")

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.run_backups", lambda config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.cleanup", lambda config: 0)
    assert main(["run"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config: 2)
    assert main(["run"]) == 2

    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_vms",
        lambda config, include_blacklisted=False: [VM("alpha", "running")],
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 3)
    assert main(["list-vms"]) == 3
    assert main(["verify"]) == 3
    assert main(["cleanup"]) == 3

    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    assert main(["list-vms", "--json"]) == 0
    assert '"alpha"' in capsys.readouterr().out
    assert main(["list-vms", "--include-blacklisted"]) == 0
    assert "alpha\trunning" in capsys.readouterr().out

    monkeypatch.setattr("libvirt_backup_system.cli.verify", lambda config, vm_name=None: 0)
    assert main(["verify", "--vm", "alpha"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.cleanup", lambda config: 0)
    assert main(["cleanup"]) == 0

    forces: list[bool] = []
    monkeypatch.setattr(
        "libvirt_backup_system.cli.restore_to_dir",
        lambda source, target, *, force=False: forces.append(force) or 0,
    )
    assert main(["restore-to-dir", "src", "tgt"]) == 0
    assert main(["restore-to-dir", "src", "tgt", "--force"]) == 0
    assert forces == [False, True]
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 4)
    assert main(["restore-to-dir", "src", "tgt"]) == 4


def test_cli_list_vms_json_keeps_env_override_logs_off_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BACKUP_COMPRESS", "false")
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_vms",
        lambda config, include_blacklisted=False: [VM("alpha", "running")],
    )

    assert main(["--prefix", str(tmp_path), "list-vms", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == '[{"name": "alpha", "state": "running", "running": true}]\n'
    assert "env override" in captured.err


def test_cli_run_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    from libvirt_backup_system.lock import LockBusyError

    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: "cfg")
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config: 0)

    @contextlib.contextmanager
    def busy(config: object):
        raise LockBusyError(tmp_path / "run.lock")
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", busy)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.run_backups",
        lambda config: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert main(["run"]) == 1
    err = capsys.readouterr().err
    assert "another run in progress" in err
    assert "run.lock" in err


def test_cli_exceptions(monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.cli.install", lambda prefix: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert main(["install"]) == 130
    assert "interrupted" in capsys.readouterr().err

    monkeypatch.setattr("libvirt_backup_system.cli.install", lambda prefix: (_ for _ in ()).throw(RuntimeError("bad")))
    assert main(["install"]) == 1
    assert "fatal error" in capsys.readouterr().err


def test_cli_fallback_help(monkeypatch) -> None:
    parser = build_parser()
    monkeypatch.setattr(
        parser,
        "parse_args",
        lambda argv=None: type("Args", (), {"command": "unknown", "config": None, "prefix": None})(),
    )
    monkeypatch.setattr("libvirt_backup_system.cli.build_parser", lambda: parser)
    assert main([]) == 2


def test_main_module(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.cli.main", lambda: 0)
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("libvirt_backup_system.__main__", run_name="__main__")
    assert exc.value.code == 0
    import libvirt_backup_system.__main__ as main_module

    assert main_module.main


def test_constants_and_version() -> None:
    assert __version__ == "0.1.0"
    assert "Description=Libvirt VM backup orchestrator" in UNIT_SERVICE
    assert "OnCalendar={calendar}" in UNIT_TIMER
    assert shutil.which("python3")
