"""Edge-case tests for ``restore.restore`` defensive paths.

Covers the unsafe-uuid / unsafe-timestamp gates, the virsh-failure paths in
the same-host overwrite flow, and the staging-dir creation guards. The happy
paths live in ``test_restore.py``; this module exists so the coverage gate
does not regress when the defensive branches are touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID

ALPHA_NAME = "alpha"


def _seed_chain(cfg: Config, stamp: str) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    chain_dir = vm_dir / "2026-05" / stamp
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    (chain_dir / "runs.jsonl").write_text(
        json.dumps({"ts": stamp, "checkpoint": f"virtnbdbackup.{stamp}"}) + "\n",
        encoding="utf-8",
    )
    return chain_dir


def _write_vmconfig(chain_dir: Path, disk_paths: list[Path]) -> None:
    disks = "\n".join(f"    <disk type='file' device='disk'><source file='{path}'/></disk>" for path in disk_paths)
    chain_dir.joinpath("vmconfig.virtnbdbackup.0.xml").write_text(
        f"<domain><name>{ALPHA_NAME}</name><devices>\n{disks}\n</devices></domain>\n",
        encoding="utf-8",
    )


def _restore_config(cfg: Config, tmp_path: Path) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    cfg.values["HOST_ID"] = "host"
    cfg.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    return cfg


def _virsh_local_vm(name: str = ALPHA_NAME) -> object:
    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, name, "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        return CommandResult(args, 0, "", "")

    return fake


def test_restore_rejects_timestamp_with_path_separator(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    assert restore(cfg, ALPHA_UUID, "../escape") == 1
    assert "timestamp is malformed" in capsys.readouterr().err


def test_restore_overwrite_destroy_command_error_is_tolerated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    # ``virsh destroy`` returning nonzero (VM already off) is logged at info
    # and the flow continues — domstate then confirms the VM is shut off.
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)

    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            raise CommandError(CommandResult(args, 1, "", "domain is not running"))
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake)
    monkeypatch.setattr("libvirt_backup_system.restore.define_restored_domain", lambda *_args: True)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: CommandResult(args, 0, "", ""),
    )
    assert restore(cfg, ALPHA_UUID, stamp) == 0


def test_restore_overwrite_destroy_oserror_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)

    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            raise FileNotFoundError(2, "virsh missing")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake)
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "virsh destroy unavailable" in capsys.readouterr().err


def test_restore_overwrite_domstate_command_error_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)

    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            return CommandResult(args, 0, "", "")
        if "domstate" in args:
            raise CommandError(CommandResult(args, 1, "", "broken"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake)
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "domstate check failed" in capsys.readouterr().err


def test_restore_overwrite_undefine_command_error_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)

    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            return CommandResult(args, 0, "", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        if "undefine" in args:
            raise CommandError(CommandResult(args, 1, "", "broken"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not restore after undefine failure")),
    )
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "undefine failed" in capsys.readouterr().err


def test_restore_overwrite_undefine_oserror_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)

    def fake(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            return CommandResult(args, 0, "", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        if "undefine" in args:
            raise FileNotFoundError(2, "virsh missing")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake)
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "virsh undefine unavailable" in capsys.readouterr().err


def test_restore_unsafe_staging_path_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)
    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", lambda _root, _path: False)
    monkeypatch.setattr("libvirt_backup_system.restore.run", lambda args, **_kw: CommandResult(args, 0, "", ""))
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "restore staging path is unsafe" in capsys.readouterr().err


def test_restore_staging_root_mkdir_failure_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)
    real_mkdir = Path.mkdir

    def fail(self: Path, *args: object, **kwargs: object) -> None:
        if "restore" in str(self) and "libvirt-backup-system" in str(self):
            raise OSError("denied")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail)
    monkeypatch.setattr("libvirt_backup_system.restore.run", lambda args, **_kw: CommandResult(args, 0, "", ""))
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "restore staging root creation failed" in capsys.readouterr().err


def test_restore_staging_dir_mkdir_failure_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)
    real_mkdir = Path.mkdir
    staging_root = tmp_path / "var/lib/libvirt-backup-system/restore"

    def fail(self: Path, *args: object, **kwargs: object) -> None:
        if self.parent == staging_root:
            raise OSError("denied")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail)
    monkeypatch.setattr("libvirt_backup_system.restore.run", lambda args, **_kw: CommandResult(args, 0, "", ""))
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "restore staging dir creation failed" in capsys.readouterr().err


def test_restore_virtnbdrestore_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run",
        lambda args, **_kw: CommandResult(args, 1, "", "no domain"),
    )

    def fail(args: list[str]) -> CommandResult:
        raise CommandError(CommandResult(args, 7, "", "bad"))

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fail)
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "restore failed" in capsys.readouterr().err


def test_restore_overwrite_virtnbdrestore_failure_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # virtnbdrestore failing in the overwrite path (after shutdown + undefine
    # already ran) must surface as a nonzero exit; the staging dir stays in
    # place so the operator can re-run or salvage partial results.
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    _seed_chain(cfg, stamp)
    monkeypatch.setattr("libvirt_backup_system.restore.run", _virsh_local_vm())

    def fail(args: list[str]) -> CommandResult:
        raise CommandError(CommandResult(args, 7, "", "virtnbdrestore broke"))

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fail)
    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "restore failed" in capsys.readouterr().err


def test_restore_overwrite_refuses_when_existing_disk_cannot_be_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = "20260507T100000"
    chain_dir = _seed_chain(cfg, stamp)
    disk_dir = tmp_path / "libvirt-images"
    disk_dir.mkdir()
    missing = disk_dir / "missing.qcow2"
    blocked = disk_dir / "blocked.qcow2"
    blocked.mkdir()
    _write_vmconfig(chain_dir, [missing, blocked])
    monkeypatch.setattr("libvirt_backup_system.restore.run", _virsh_local_vm())
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not restore when old disk removal fails")),
    )

    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "could not remove existing disk file" in capsys.readouterr().err
