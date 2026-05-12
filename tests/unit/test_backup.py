from __future__ import annotations

import datetime as dt
from pathlib import Path

from libvirt_backup_system import backup
from libvirt_backup_system.backup import (
    backup_vm,
    current_month,
    run_backups,
    timestamp,
)
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import virtnbdbackup_fake_success


def _backup_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
        }
    )
    return cfg


def test_time_helpers() -> None:
    now = dt.datetime(2026, 5, 7, 10, 11, 12, 345678, tzinfo=dt.timezone.utc)
    assert current_month(now) == "2026-05"
    assert timestamp(now) == "20260507T101112_345678Z"
    assert len(current_month()) == 7
    assert timestamp().endswith("Z")


def test_backup_vm_running_success(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert calls == [
        [
            "virtnbdbackup",
            "-U",
            "qemu:///system",
            "-d",
            "alpha",
            "-l",
            "full",
            "-o",
            str(tmp_path / "backups/host/alpha/2026-05/stamp"),
            "--compress",
        ]
    ]


def test_backup_vm_without_compression(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_COMPRESS"] = "false"
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return virtnbdbackup_fake_success(args, check=check, env=env)

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert "--compress" not in calls[0]


def test_backup_vm_inactive_marker_and_failure(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)
    monkeypatch.setattr("libvirt_backup_system.backup.inactive_marker_is_fresh", lambda uri, name, marker: True)
    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    legacy_fingerprint = marker.parent / ".inactive-copy-fingerprint"
    # Stamp and fingerprint are stored together in one atomic marker file.
    assert marker.read_text(encoding="utf-8") == "stamp\nfp-stub\n"
    assert not legacy_fingerprint.exists()
    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "new")
    assert "inactive VM already copied" in capsys.readouterr().out

    cfg.values["INACTIVE_COPY_EVERY_RUN"] = "true"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 9, "", "bad"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "new")
    assert "backup failed" in capsys.readouterr().err


def test_backup_vm_redoes_inactive_when_marker_is_stale(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    (marker.parent / "old").mkdir()
    marker.write_text("old\nold-fp\n", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return virtnbdbackup_fake_success(args, check=check, env=env)

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.inactive_marker_is_fresh", lambda uri, name, m: False)

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert calls
    assert calls[0][:1] == ["virtnbdbackup"]
    assert "inactive marker is stale" in capsys.readouterr().out
    assert marker.read_text(encoding="utf-8") == "stamp\nfp-stub\n"


def test_backup_vm_clears_marker_when_vm_running(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/alpha/2026-05/.inactive-copy-complete"
    fingerprint = marker.parent / ".inactive-copy-fingerprint"
    marker.parent.mkdir(parents=True)
    marker.write_text("old\n", encoding="utf-8")
    fingerprint.write_text("oldfp\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)

    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert not marker.exists()
    assert not fingerprint.exists()


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


def test_backup_vm_partial_backup_removal_failure_surfaced(
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

    def failing_rmtree(path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.shutil.rmtree", failing_rmtree)

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "partial backup removal failed" in err
    assert "permission denied" in err
    assert dest.exists()


def test_backup_vm_partial_backup_removal_incomplete_surfaced(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / "backups/host/alpha/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        raise CommandError(CommandResult(args, 7, "", "boom"))

    def lying_rmtree(path: Path) -> None:
        return None

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.shutil.rmtree", lying_rmtree)

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert "partial backup removal incomplete" in capsys.readouterr().err
    assert dest.exists()


def test_backup_vm_rejects_symlinked_backup_subpath(tmp_path: Path, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    backup_path = cfg.path_value("BACKUP_PATH")
    outside = tmp_path / "outside/alpha"
    outside.mkdir(parents=True)
    (backup_path / "host").mkdir(parents=True)
    (backup_path / "host/alpha").symlink_to(outside, target_is_directory=True)

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert not (outside / "2026-05").exists()
    assert "backup skipped because destination is unsafe" in capsys.readouterr().err


def test_backup_subpath_rejects_empty_backup_path(tmp_path: Path, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_PATH"] = ""

    assert not backup.backup_subpath_is_safe(cfg, tmp_path / "anything")


def test_backup_vm_stops_if_created_destination_becomes_unsafe(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    checks = iter([True, False])

    monkeypatch.setattr("libvirt_backup_system.backup.backup_subpath_is_safe", lambda config, path: next(checks))
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("backup should not run")),
    )

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert "backup skipped because destination became unsafe" in capsys.readouterr().err


def test_backup_vm_leaves_unsafe_partial_destination_on_failure(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / "backups/host/alpha/2026-05/stamp"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest.mkdir(parents=True, exist_ok=True)
        raise CommandError(CommandResult(args, 7, "", "boom"))

    def fake_safe(config: Config, path: Path) -> bool:
        return path != dest

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.backup_subpath_is_safe", fake_safe)

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert dest.exists()
    assert "partial backup removal skipped because destination is unsafe" in capsys.readouterr().err


def test_run_backups_success_and_failures(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.list_vms", lambda config: [VM("alpha", "running"), VM("beta", "running")]
    )
    monkeypatch.setattr("libvirt_backup_system.backup.backup_vm", lambda config, vm, month, stamp: vm.name == "alpha")
    assert run_backups(cfg) == 1

    monkeypatch.setattr("libvirt_backup_system.backup.backup_vm", lambda config, vm, month, stamp: True)
    assert run_backups(cfg) == 0


def test_backup_vm_rejects_unsafe_vm_name(backup_config) -> None:
    cfg = _backup_config(backup_config)
    import pytest

    for unsafe in ("-evil", "..", "a/b", "back\\slash", ""):
        with pytest.raises(ValueError, match="unsafe VM name"):
            backup_vm(cfg, VM(unsafe, "running"), "2026-05", "stamp")
    assert backup.current_month(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)) == "2026-01"
