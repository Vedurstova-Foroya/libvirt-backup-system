from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID, virtnbdbackup_fake_success


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true"})
    return cfg


def test_backup_vm_fails_when_fingerprint_cannot_be_computed(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: None)

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "domain XML fingerprint computation failed" in capsys.readouterr().err


def test_backup_vm_bails_when_required_nfs_mount_disappears(tmp_path: Path, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_backup_vm_proceeds_when_required_nfs_mount_present(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    monkeypatch.setattr("libvirt_backup_system.paths.Path.is_mount", lambda self: True)
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)

    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")


def test_backup_vm_bails_when_mount_disappears_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    # Entry check passes; the recheck right before mkdir flips to False.
    checks = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.paths.Path.is_mount", lambda self: next(checks, False))
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_backup_vm_rejects_zero_exit_with_missing_destination(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # virtnbdbackup returning 0 without producing the output directory must be
    # treated as failure so a hollow write is never recorded as a real backup.
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "backup reported success but destination is missing" in err


def test_backup_vm_skips_when_month_dir_mkdir_fails(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    original_mkdir = Path.mkdir
    target = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05"

    def fake_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if self == target:
            raise OSError("quota exceeded")
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.backup.Path.mkdir", fake_mkdir)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "backup skipped because month directory creation failed" in err
    assert "quota exceeded" in err


def test_backup_vm_rejects_dest_replaced_by_symlink_post_copy(tmp_path, monkeypatch, capsys, backup_config) -> None:
    # ``dest.is_dir()`` follows a swapped-in symlink, so a symlink race between
    # pre-flight and virtnbdbackup's exit must be caught by re-running subpath
    # safety on dest.
    cfg = _backup_config(backup_config)
    outside = tmp_path / "outside"
    outside.mkdir()

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        dest = Path(args[args.index("-o") + 1])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(outside, target_is_directory=True)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "backup destination became unsafe after virtnbdbackup" in capsys.readouterr().err


def test_backup_vm_disable_path_skips_cleanup_when_dest_never_created(
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # ``virtnbdbackup`` can fail before the dest directory is materialised
    # (binary missing, AppArmor deny, ENOSPC before mkdir, ...). The post-
    # failure cleanup path must observe that the destination does not exist
    # and skip both partial cleanup and chain poisoning rather than logging a
    # spurious failure.
    cfg = _backup_config(backup_config)

    def never_creates_dest(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 9, "", "boom"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", never_creates_dest)
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "backup failed" in err
    assert "partial backup removal" not in err


def test_backup_vm_fails_when_initial_checkpoint_read_fails(monkeypatch, capsys, backup_config) -> None:
    # ``list_checkpoints`` is called once before virtnbdbackup so the run can
    # detect new checkpoints by diffing the set afterward. If that initial
    # read raises ``CheckpointReadError`` (NFS hiccup, permission flip),
    # backup_vm must fail closed instead of recording state we cannot trust.
    from libvirt_backup_system.run_records import CheckpointReadError

    cfg = _backup_config(backup_config)

    def fail_read(chain_dir, vm_name=None):
        raise CheckpointReadError("denied")

    monkeypatch.setattr("libvirt_backup_system.backup.list_checkpoints", fail_read)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "checkpoint metadata read failed" in capsys.readouterr().err


def test_backup_vm_running_revalidates_nfs_after_copy(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    # Pre-copy checks pass; the post-copy revalidation sees the mount gone.
    checks = iter([True, True, False])
    monkeypatch.setattr("libvirt_backup_system.paths.Path.is_mount", lambda self: next(checks, False))
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "backup completed but backup path no longer mounted" in err
