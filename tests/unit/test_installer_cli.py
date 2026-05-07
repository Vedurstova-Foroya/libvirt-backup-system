from __future__ import annotations

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
    assert bin_path.exists()
    assert config_path.exists()
    assert "ExecStart=/usr/local/bin/libvirt-backup-system run" in (
        tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    ).read_text(encoding="utf-8")
    assert "OnCalendar=" in (tmp_path / "etc/systemd/system/libvirt-backup-system.timer").read_text(encoding="utf-8")

    (tmp_path / "var/lib/libvirt-backup-system").mkdir(parents=True)
    (tmp_path / "var/log/libvirt-backup-system").mkdir(parents=True)
    assert uninstall(str(tmp_path)) == 0
    assert not bin_path.exists()
    assert config_path.exists()

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "LOCAL_ROOT=/var/backups/libvirt",
            f"LOCAL_ROOT={tmp_path / 'backups'}",
        ),
        encoding="utf-8",
    )
    assert install(str(tmp_path)) == 0
    assert uninstall(str(tmp_path), purge_config=True, purge_state=True, purge_logs=True, purge_backups=True) == 0
    assert not config_path.exists()


def test_install_replaces_existing_package_and_systemd_activation(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_config(prefix: str | None = None) -> Config:
        return Config(values=dict(DEFAULTS), path=tmp_path / "etc/config.env", prefix=tmp_path)

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


def test_uninstall_purges_file_path(tmp_path: Path) -> None:
    state_file = tmp_path / "var/lib/libvirt-backup-system"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("state\n", encoding="utf-8")
    assert uninstall(str(tmp_path), purge_state=True) == 0
    assert not state_file.exists()


def test_cli_commands(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.cli.install", lambda prefix: 11)
    assert main(["--prefix", str(tmp_path), "install"]) == 11

    monkeypatch.setattr("libvirt_backup_system.cli.uninstall", lambda prefix, **kwargs: 12)
    assert main(["--prefix", str(tmp_path), "uninstall", "--purge-config"]) == 12

    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: "cfg")
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config: 0)
    assert main(["preflight"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.run_backups", lambda config: 0)
    assert main(["run"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config: 2)
    assert main(["run"]) == 2

    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_vms",
        lambda config, include_blacklisted=False: [VM("alpha", "running")],
    )
    assert main(["list-vms", "--json"]) == 0
    assert '"alpha"' in capsys.readouterr().out
    assert main(["list-vms", "--include-blacklisted"]) == 0
    assert "alpha\trunning" in capsys.readouterr().out

    monkeypatch.setattr("libvirt_backup_system.cli.verify", lambda config, vm_name=None: 0)
    assert main(["verify", "--vm", "alpha"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.cleanup", lambda config: 0)
    assert main(["cleanup"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.restore_to_dir", lambda source, target: 0)
    assert main(["restore-to-dir", "source", "target"]) == 0


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
