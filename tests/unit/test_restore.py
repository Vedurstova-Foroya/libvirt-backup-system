from __future__ import annotations

import datetime as dt
from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import parse_at, restore
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def _seed_chain(cfg: Config, months_and_chains: dict[str, list[str]]) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    vm_dir.mkdir(parents=True, exist_ok=True)
    for month, chains in months_and_chains.items():
        (vm_dir / month).mkdir(exist_ok=True)
        for chain in chains:
            (vm_dir / month / chain).mkdir()
    return vm_dir


def _restore_config(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def _stamp(month: str, day: int, hour: int = 12) -> str:
    return f"{month.replace('-', '')}{day:02d}T{hour:02d}0000"


def test_restore_picks_latest_snapshot_across_months(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    older = _stamp("2025-12", 1, 8)
    newer = _stamp("2026-01", 3, 9)
    _seed_chain(cfg, {"2025-12": [older], "2026-01": [_stamp("2026-01", 2), newer]})
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )

    output = tmp_path / "out"
    assert restore(cfg, ALPHA_UUID, output) == 0

    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2026-01" / newer
    assert captured == [["virtnbdrestore", "-a", "restore", "-i", str(expected), "-o", str(output)]]
    assert output.is_dir()


def test_restore_at_picks_closest_snapshot_going_backwards(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    # Three snapshots: Dec 1 08:00, Dec 31 12:00, Jan 3 09:00. Targeting
    # Jan 1 03:00 must roll back to Dec 31 12:00, not forward to Jan 3 09:00.
    cfg = _restore_config(backup_config)
    dec_1 = _stamp("2025-12", 1, 8)
    dec_31 = _stamp("2025-12", 31, 12)
    jan_3 = _stamp("2026-01", 3, 9)
    _seed_chain(cfg, {"2025-12": [dec_1, dec_31], "2026-01": [jan_3]})
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-01-01T03:00:00") == 0

    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2025-12" / dec_31
    assert captured[0][captured[0].index("-i") + 1] == str(expected)


def test_restore_at_earlier_than_oldest_snapshot_errors(tmp_path: Path, backup_config: Config, capsys) -> None:
    # Pinning to a time before any backup existed must be an explicit error,
    # not silently restore the oldest available chain (which would be a lie
    # about what state the operator is recovering).
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2025-01-01") == 1
    assert "earlier than the oldest backup" in capsys.readouterr().err


def test_restore_at_accepts_compact_stamp(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    # ``--at`` accepts the exact chain dir name so operators can copy a stamp
    # from a directory listing and pin to it verbatim.
    cfg = _restore_config(backup_config)
    older = _stamp("2026-01", 5, 8)
    newer = _stamp("2026-01", 5, 12)
    _seed_chain(cfg, {"2026-01": [older, newer]})
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at=older) == 0
    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2026-01" / older
    assert captured[0][captured[0].index("-i") + 1] == str(expected)


def test_restore_at_date_rolls_to_end_of_target_day(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    # A bare ``YYYY-MM-DD`` resolves to midnight UTC of that day. A snapshot
    # taken later the same day is *after* the target, so the previous day's
    # snapshot wins.
    cfg = _restore_config(backup_config)
    morning = _stamp("2026-01", 5, 8)
    afternoon = _stamp("2026-01", 5, 14)
    _seed_chain(cfg, {"2026-01": [morning, afternoon]})
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-01-05T09:00:00") == 0
    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2026-01" / morning
    assert captured[0][captured[0].index("-i") + 1] == str(expected)


def test_restore_at_malformed(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="not-a-time") == 1
    assert "restore --at is malformed" in capsys.readouterr().err


def test_parse_at_handles_supported_formats() -> None:
    assert parse_at("2026-05-07") == dt.datetime(2026, 5, 7, tzinfo=dt.timezone.utc)
    assert parse_at("2026-05-07T10:11:12") == dt.datetime(2026, 5, 7, 10, 11, 12, tzinfo=dt.timezone.utc)
    assert parse_at("20260507T101112") == dt.datetime(2026, 5, 7, 10, 11, 12, tzinfo=dt.timezone.utc)
    # Aware timestamps are converted to UTC for chain comparison.
    aware = parse_at("2026-05-07T13:11:12+03:00")
    assert aware == dt.datetime(2026, 5, 7, 10, 11, 12, tzinfo=dt.timezone.utc)
    assert parse_at("garbage") is None
    assert parse_at("   ") is None


def test_restore_rejects_existing_non_empty_output(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    output = tmp_path / "out"
    output.mkdir()
    (output / "leftover").write_bytes(b"x")
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is not empty" in capsys.readouterr().err


def test_restore_allows_empty_existing_output(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: CommandResult(args, 0, "", ""),
    )
    assert restore(cfg, ALPHA_UUID, output) == 0


def test_restore_invalid_vm_name(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    assert restore(cfg, "../escape", tmp_path / "out") == 1
    assert "restore target name is invalid" in capsys.readouterr().err


def test_restore_missing_vm(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    (cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")).mkdir()
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: None)
    assert restore(cfg, "missing", tmp_path / "out") == 1
    assert "restore target not found" in capsys.readouterr().err


def test_restore_resolved_uuid_dir_missing(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    (cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")).mkdir(parents=True)
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: ALPHA_UUID)
    assert restore(cfg, "alpha", tmp_path / "out") == 1
    assert "restore target not found" in capsys.readouterr().err


def test_restore_resolves_vm_name_via_virsh(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: ALPHA_UUID)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: CommandResult(args, 0, "", ""),
    )
    assert restore(cfg, "alpha", tmp_path / "out") == 0


def test_restore_no_snapshots_available(tmp_path: Path, backup_config: Config, capsys) -> None:
    # Every shape of "no real chain dir" must surface as the same "no backups"
    # error: an empty VM root, a month dir with no chain dirs inside, a stray
    # file under VM root that is not a month dir, a stray file inside a month
    # dir, an unsafe (dot-prefixed) chain name, and a chain dir whose name is
    # not a timestamp at all (e.g. ``not-a-stamp``).
    cfg = _restore_config(backup_config)
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    vm_dir.mkdir(parents=True)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore found no backups" in capsys.readouterr().err

    (vm_dir / "README").write_text("operator notes", encoding="utf-8")
    (vm_dir / "stray-dir").mkdir()
    (vm_dir / "2026-01").mkdir()
    assert restore(cfg, ALPHA_UUID, tmp_path / "out2") == 1
    assert "restore found no backups" in capsys.readouterr().err

    (vm_dir / "2026-01" / "stray-file").write_text("noise", encoding="utf-8")
    (vm_dir / "2026-01" / ".hidden-name").mkdir()
    (vm_dir / "2026-01" / "not-a-stamp").mkdir()
    assert restore(cfg, ALPHA_UUID, tmp_path / "out3") == 1
    assert "restore found no backups" in capsys.readouterr().err


def test_restore_command_failure(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})

    def fail(args: list[str]) -> CommandResult:
        raise CommandError(CommandResult(args, 7, "", "bad"))

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fail)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore failed" in capsys.readouterr().err


def test_restore_refuses_when_mount_missing(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_restore_refuses_unsafe_backup_root(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", lambda root, path: False)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because backup root is unsafe" in capsys.readouterr().err


def test_restore_output_creation_failure(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})

    def fail(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.restore.Path.mkdir", fail)
    assert restore(cfg, ALPHA_UUID, tmp_path / "deep" / "out") == 1
    assert "restore output directory creation failed" in capsys.readouterr().err


def test_restore_output_not_a_directory(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    output = tmp_path / "file-not-dir"
    output.write_bytes(b"data")
    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is not a usable directory" in capsys.readouterr().err


def test_restore_chain_path_unsafe(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    host_root = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")
    safe_paths = {host_root, vm_dir}

    def fake_safe(root: Path, path: Path) -> bool:
        return path in safe_paths

    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", fake_safe)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because chain path is unsafe" in capsys.readouterr().err


def test_restore_vm_root_path_unsafe(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = _seed_chain(cfg, {"2026-01": [_stamp("2026-01", 5)]})
    host_root = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")

    def fake_safe(root: Path, path: Path) -> bool:
        return path == host_root

    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", fake_safe)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because VM root is unsafe" in capsys.readouterr().err
    assert vm_dir.is_dir()
