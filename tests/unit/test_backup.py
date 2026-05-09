from __future__ import annotations

import datetime as dt
from pathlib import Path

from libvirt_backup_system import backup
from libvirt_backup_system.backup import (
    backup_vm,
    cleanup,
    current_month,
    restore_to_dir,
    run_backups,
    timestamp,
    verify,
)
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM


def _backup_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
            "BACKUP_RETENTION_MONTHS": "1",
        }
    )
    return cfg


def test_time_helpers() -> None:
    now = dt.datetime(2026, 5, 7, 10, 11, 12, tzinfo=dt.timezone.utc)
    assert current_month(now) == "2026-05"
    assert timestamp(now) == "20260507T101112Z"
    assert len(current_month()) == 7
    assert timestamp().endswith("Z")


def test_backup_vm_running_success(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert calls == [
        [
            "virtnbdbackup",
            "-d",
            "alpha",
            "-l",
            "auto",
            "-o",
            str(tmp_path / "backups/host/alpha/2026-05/stamp"),
            "--compress",
        ]
    ]


def test_backup_vm_without_compression(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_COMPRESS"] = "false"
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert "--compress" not in calls[0]


def test_backup_vm_inactive_marker_and_failure(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    assert marker.read_text(encoding="utf-8") == "stamp\n"
    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "new")
    assert "inactive VM already copied" in capsys.readouterr().out

    cfg.values["INACTIVE_COPY_EVERY_RUN"] = "true"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 9, "", "bad"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "new")
    assert "backup failed" in capsys.readouterr().err


def test_backup_vm_removes_partial_destination_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / "backups/host/alpha/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "partial").write_bytes(b"junk")
        raise CommandError(CommandResult(args, 7, "", "boom"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert not dest.exists()
    err = capsys.readouterr()
    assert "removed partial backup" in err.out
    assert "backup failed" in err.err


def test_run_backups_success_and_failures(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.list_vms", lambda config: [VM("alpha", "running"), VM("beta", "running")]
    )
    monkeypatch.setattr("libvirt_backup_system.backup.backup_vm", lambda config, vm, month, stamp: vm.name == "alpha")
    assert run_backups(cfg) == 1

    monkeypatch.setattr("libvirt_backup_system.backup.backup_vm", lambda config, vm, month, stamp: True)
    assert run_backups(cfg) == 0


def test_cleanup_zero_retention(tmp_path: Path, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    (tmp_path / "backups/host/alpha/2026-01").mkdir(parents=True)
    assert cleanup(cfg) == 0
    assert not (tmp_path / "backups/host/alpha/2026-01").exists()


def test_verify_success_failure_and_vm_filter(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    good = tmp_path / "backups/host/alpha/2026-05/good"
    bad = tmp_path / "backups/host/alpha/2026-05/bad"
    good.mkdir(parents=True)
    bad.mkdir()

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if args[2] in {str(bad), str(tmp_path / "backups/host/alpha/2026-05/was-bad")}:
            raise CommandError(CommandResult(args, 2, "", "bad"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert verify(cfg, vm_name="alpha") == 1
    bad.rename(tmp_path / "backups/host/alpha/2026-05/was-bad")
    assert verify(cfg) == 1
    assert verify(cfg, vm_name="missing") == 0


def test_backup_vm_rejects_vm_name_starting_with_dash(backup_config) -> None:
    cfg = _backup_config(backup_config)
    import pytest

    with pytest.raises(ValueError, match="begins with a dash"):
        backup_vm(cfg, VM("-evil", "running"), "2026-05", "stamp")


def test_restore_to_dir(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    assert restore_to_dir("source", str(tmp_path / "restore")) == 0
    assert (tmp_path / "restore").is_dir()
    assert calls == [["virtnbdrestore", "-i", "source", "-o", "restore", "-D", str(tmp_path / "restore")]]


def test_module_import() -> None:
    assert backup.current_month(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)) == "2026-01"
