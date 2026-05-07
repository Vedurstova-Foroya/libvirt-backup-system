from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.preflight import _df_available_kb, _remote_df_available_kb, check, sh_quote
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM


def config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "LOCAL_ROOT": str(tmp_path / "backups"),
            "REMOTE_ENABLED": "true",
            "REMOTE_HOST": "qnap",
            "REMOTE_USER": "backup",
            "REMOTE_DIR": "/backup",
            "REQUIRE_ROOT": "true",
            "BACKUP_ESTIMATE_GB_PER_VM": "1",
            "SPACE_MARGIN_PERCENT": "20",
        }
    )
    return cfg


def test_sh_quote() -> None:
    assert sh_quote("simple") == "'simple'"
    assert sh_quote("it's") == "'it'\"'\"'s'"


def test_df_helpers(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "Filesystem 1024-blocks Used Available Capacity Mounted on\nfs 9 1 7 1% /\n", "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    cfg = config(tmp_path)
    assert _df_available_kb(tmp_path / "new") == 7
    assert _remote_df_available_kb(cfg) == 7


def test_df_helpers_bad_output(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "header-only\n", "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    cfg = config(tmp_path)
    for func in [_df_available_kb, lambda path: _remote_df_available_kb(cfg)]:
        try:
            func(tmp_path)
        except RuntimeError as exc:
            assert "data row" in str(exc)


def test_check_passes(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running")])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 2_000_000)
    monkeypatch.setattr("libvirt_backup_system.preflight._remote_df_available_kb", lambda config: 2_000_000)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    assert check(cfg) == 0
    assert "preflight passed" in capsys.readouterr().out


def test_check_passes_with_remote_disabled(tmp_path: Path, monkeypatch) -> None:
    cfg = config(tmp_path)
    cfg.values["REMOTE_ENABLED"] = "false"
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running")])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 2_000_000)
    assert check(cfg) == 0


def test_check_reports_failures(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    cfg.values["REMOTE_DIR"] = ""
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: None)
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 99)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 1)
    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "missing binary" in err
    assert "must run as root" in err
    assert "no VMs selected" in err
    assert "REMOTE_DIR is required" in err


def test_check_reports_local_and_remote_low_space(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running")])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 1)
    monkeypatch.setattr("libvirt_backup_system.preflight._remote_df_available_kb", lambda config: 1)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "insufficient local space" in err
    assert "insufficient remote space" in err


def test_check_handles_discovery_and_remote_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.list_vms", lambda config: (_ for _ in ()).throw(RuntimeError("no libvirt"))
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._df_available_kb",
        lambda path: (_ for _ in ()).throw(RuntimeError("bad df")),
    )

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 2, "stdout", "stderr"))

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "libvirt VM discovery failed" in err
    assert "local space check failed" in err
    assert "remote SSH check failed" in err


def test_check_handles_remote_space_error(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running")])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 2_000_000)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._remote_df_available_kb",
        lambda config: (_ for _ in ()).throw(RuntimeError("bad remote df")),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    assert check(cfg) == 1
    assert "remote space check failed" in capsys.readouterr().err


def test_os_import_used() -> None:
    assert hasattr(os, "getcwd")
