from __future__ import annotations

import contextlib
import runpy
import shutil
from pathlib import Path

import pytest

from libvirt_backup_system import __version__
from libvirt_backup_system.cli import build_parser, main
from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import UNIT_SERVICE, UNIT_TIMER
from libvirt_backup_system.kopia_password import PasswordSpec
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _fake_config(tmp_path: Path) -> Config:
    return Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)


def test_cli_commands(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.cli.install",
        lambda prefix, config_path=None, password_spec=None: 11,
    )
    assert main(["--prefix", str(tmp_path), "install"]) == 11

    monkeypatch.setattr("libvirt_backup_system.cli.uninstall", lambda prefix, **kwargs: 12)
    assert main(["--prefix", str(tmp_path), "uninstall", "--purge-config"]) == 12

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config, *, lock_held=False: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    assert main(["check"]) == 0
    assert main(["preflight"]) == 0

    @contextlib.contextmanager
    def fake_lock(config: object):
        yield Path("/tmp/fake.lock")

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.run_backups", lambda config: 0)
    assert main(["run"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config, *, lock_held=False: 2)
    assert main(["run"]) == 2

    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_vms",
        lambda config, include_blacklisted=False: [VM("alpha", "running", ALPHA_UUID)],
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 3)
    assert main(["list-vms"]) == 3
    assert main(["verify"]) == 3

    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    assert main(["list-vms", "--json"]) == 0
    assert '"alpha"' in capsys.readouterr().out
    assert main(["list-vms", "--include-blacklisted"]) == 0
    assert "alpha\trunning" in capsys.readouterr().out

    monkeypatch.setattr("libvirt_backup_system.cli.verify", lambda config, *, include_hosts=None: 0)
    assert main(["verify"]) == 0

    monkeypatch.setattr("libvirt_backup_system.cli.restore", lambda config, vm_uuid, timestamp, **kwargs: 4)
    assert main(["restore", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "20260507T101112"]) == 4


def test_cli_run_returns_backup_code(tmp_path: Path, monkeypatch) -> None:
    # Kopia engine: maintenance/retention run on a separate timer, so the
    # ``run`` command's exit code is just the backup exit code.
    @contextlib.contextmanager
    def fake_lock(config: object):
        yield Path("/tmp/fake.lock")

    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config, *, lock_held=False: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.run_backups", lambda config: 0)
    assert main(["run"]) == 0
    monkeypatch.setattr("libvirt_backup_system.cli.run_backups", lambda config: 2)
    assert main(["run"]) == 2


def test_cli_restore_reports_validate_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 7)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.restore",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run when validate fails")),
    )
    assert main(["restore", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "20260507T101112"]) == 7


def test_cli_restore_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    # Restore reads only, but a concurrent ``run`` could be writing into the
    # latest chain dir — surface "another run in progress" rather than racing.
    from libvirt_backup_system.lock import LockBusyError

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)

    @contextlib.contextmanager
    def busy(config: object):
        raise LockBusyError(tmp_path / "run.lock")
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", busy)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.restore",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("restore must not run while lock is busy")),
    )
    assert main(["restore", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "20260507T101112"]) == 1
    assert "another run in progress" in capsys.readouterr().err


def test_cli_list_vms_json_keeps_env_override_logs_off_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    # Set a key in CONFIG_KEYS so Config.load logs an "env override" event on
    # stderr; the test then asserts that diagnostic does not bleed onto stdout.
    monkeypatch.setenv("SYSTEMD_ON_CALENDAR", "*-*-* 03:30:00")
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_vms",
        lambda config, include_blacklisted=False: [VM("alpha", "running", ALPHA_UUID)],
    )

    assert main(["--prefix", str(tmp_path), "list-vms", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ('[{"name": "alpha", "uuid": "' + ALPHA_UUID + '", "state": "running", "running": true}]\n')
    assert "env override" in captured.err


def test_cli_reports_invalid_command_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _fake_config(tmp_path)
    cfg.values["COMMAND_TIMEOUT_SECONDS"] = "0"
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    assert main(["check"]) == 1
    assert "command timeout must be greater than 0" in capsys.readouterr().err


def test_cli_status_dispatches_to_systemd_units(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_status(prefix: str | None) -> int:
        captured["prefix"] = prefix
        return 7

    monkeypatch.setattr("libvirt_backup_system.cli.status", fake_status)
    assert main(["--prefix", "/x", "status"]) == 7
    assert captured["prefix"] == "/x"


def test_cli_install_and_uninstall_forward_config_path(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_install(
        prefix: str | None,
        *,
        config_path: str | None = None,
        password_spec: object | None = None,
    ) -> int:
        captured["install"] = (prefix, config_path, password_spec)
        return 0

    def fake_uninstall(prefix: str | None, **kwargs: object) -> int:
        captured["uninstall"] = (prefix, kwargs)
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.install", fake_install)
    monkeypatch.setattr("libvirt_backup_system.cli.uninstall", fake_uninstall)
    custom = str(tmp_path / "custom.env")
    assert main(["--config", custom, "--prefix", str(tmp_path), "install"]) == 0
    captured_install = captured["install"]
    assert isinstance(captured_install, tuple)
    assert captured_install[:2] == (str(tmp_path), custom)
    spec = captured_install[2]
    assert isinstance(spec, PasswordSpec)
    assert spec.acknowledge_loss is False
    assert main(["--config", custom, "--prefix", str(tmp_path), "install", "--acknowledge-password-loss"]) == 0
    spec = captured["install"][2]  # type: ignore[index]
    assert isinstance(spec, PasswordSpec)
    assert spec.acknowledge_loss is True
    assert main(["--config", custom, "--prefix", str(tmp_path), "uninstall", "--purge-logs"]) == 0
    prefix, kwargs = captured["uninstall"]  # type: ignore[misc]
    assert prefix == str(tmp_path)
    assert kwargs["config_path"] == custom
    assert kwargs["purge_logs"] is True


def test_cli_run_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    from libvirt_backup_system.lock import LockBusyError

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.check", lambda config, *, lock_held=False: 0)

    @contextlib.contextmanager
    def busy(config: object):
        raise LockBusyError(tmp_path / "run.lock")
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", busy)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.run_backups",
        lambda config, *, month=None: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert main(["run"]) == 1
    err = capsys.readouterr().err
    assert "another run in progress" in err
    assert "run.lock" in err


def test_cli_verify_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    # verify holds the same run-lock as run: a concurrent backup could
    # otherwise expose a half-written backup directory and produce a confusing
    # virtnbdrestore error instead of a clean "another run in progress".
    from libvirt_backup_system.lock import LockBusyError

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)

    @contextlib.contextmanager
    def busy(config: object):
        raise LockBusyError(tmp_path / "run.lock")
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", busy)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.verify",
        lambda config, vm_name=None: (_ for _ in ()).throw(AssertionError("verify must not run while lock is busy")),
    )
    assert main(["verify"]) == 1
    err = capsys.readouterr().err
    assert "another run in progress" in err
    assert "run.lock" in err


def test_cli_exceptions(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.cli.install",
        lambda prefix, config_path=None, password_spec=None: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert main(["install"]) == 130
    assert "interrupted" in capsys.readouterr().err

    monkeypatch.setattr(
        "libvirt_backup_system.cli.install",
        lambda prefix, config_path=None, password_spec=None: (_ for _ in ()).throw(RuntimeError("bad")),
    )
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
    # Description is parameterized so the same template renders both the run
    # and check service units; the rendered string is exercised end-to-end in
    # test_installer.test_install_and_uninstall_preserves_and_purges.
    assert "Description={description}" in UNIT_SERVICE
    assert "OnCalendar={calendar}" in UNIT_TIMER
    assert shutil.which("python3")
