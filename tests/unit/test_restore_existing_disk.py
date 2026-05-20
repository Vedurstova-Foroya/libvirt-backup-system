from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.cli import build_parser
from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def _seed_chain(cfg: Config, stamp: str) -> None:
    chain_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2026-05" / stamp
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    (chain_dir / "runs.jsonl").write_text(
        json.dumps({"ts": stamp, "checkpoint": f"virtnbdbackup.{stamp}"}) + "\n",
        encoding="utf-8",
    )


def test_restore_surfaces_existing_target_disk_as_final_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    backup_config.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    stamp = "20260507T100000"
    _seed_chain(backup_config, stamp)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run",
        lambda args, **_kw: CommandResult(args, 1, "", "no domain"),
    )

    def fail(args: list[str]) -> CommandResult:
        raise CommandError(CommandResult(args, 1, "", "Target file already exists: [/vm/vm-0.qcow2], won't overwrite."))

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fail)

    assert restore(backup_config, ALPHA_UUID, stamp) == 1
    last_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "restore target disk already exists" in last_line
    assert "/vm/vm-0.qcow2" in last_line


def test_restore_quiet_mode_suppresses_streamed_output_but_keeps_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    backup_config.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    stamp = "20260507T100000"
    _seed_chain(backup_config, stamp)

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        if args[0] == "virtnbdrestore":
            raise CommandError(CommandResult(args, 1, "", "Target file already exists: [/vm/vm-0.qcow2]"))
        return CommandResult(args, 1, "", "no domain")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("quiet restore must not stream command output")),
    )

    assert restore(backup_config, ALPHA_UUID, stamp, verbose=False) == 1
    err = capsys.readouterr().err
    assert "command output" not in err
    assert "rerun with --verbose" in err
    assert "restore target disk already exists" in err


def test_restore_quiet_overwrite_suppresses_start_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    backup_config.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    stamp = "20260507T100000"
    _seed_chain(backup_config, stamp)

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "vm-0", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    monkeypatch.setattr("libvirt_backup_system.restore.define_restored_domain", lambda *_args: True)

    assert restore(backup_config, ALPHA_UUID, stamp, verbose=False) == 0
    assert "restore overwrite started" not in capsys.readouterr().out


def test_restore_turnkey_returns_one_when_define_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    backup_config.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    stamp = "20260507T100000"
    _seed_chain(backup_config, stamp)

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        if args[0] == "virtnbdrestore":
            return CommandResult(args, 0, "", "")
        raise CommandError(CommandResult(args, 1, "", "no domain"))

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    monkeypatch.setattr("libvirt_backup_system.restore.define_restored_domain", lambda *_args: False)

    assert restore(backup_config, ALPHA_UUID, stamp, verbose=False) == 1


def test_restore_overwrite_returns_one_when_define_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    backup_config.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    stamp = "20260507T100000"
    _seed_chain(backup_config, stamp)

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "vm-0", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    monkeypatch.setattr("libvirt_backup_system.restore.define_restored_domain", lambda *_args: False)

    assert restore(backup_config, ALPHA_UUID, stamp, verbose=False) == 1


def test_restore_verbose_flag_defaults_off() -> None:
    parser = build_parser()
    quiet = parser.parse_args(["restore", ALPHA_UUID, "20260507T100000"])
    verbose = parser.parse_args(["restore", "--verbose", ALPHA_UUID, "20260507T100000"])

    assert not quiet.verbose
    assert verbose.verbose
