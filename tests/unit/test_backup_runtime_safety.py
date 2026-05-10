from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm, cleanup
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
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


def test_backup_vm_fails_when_inactive_fingerprint_cannot_be_computed(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: None)

    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert "inactive fingerprint computation failed" in capsys.readouterr().err


def test_backup_vm_fails_when_marker_write_fails(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.write_marker",
        lambda marker, stamp, fingerprint, vm: False,
    )

    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    capsys.readouterr()


def test_backup_vm_skips_marker_when_domain_xml_changes_during_backup(
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    fingerprints = iter(["pre-fp", "post-fp"])
    monkeypatch.setattr(
        "libvirt_backup_system.backup.domain_xml_fingerprint",
        lambda uri, name: next(fingerprints),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    written: list[tuple[str, str]] = []

    def fake_write(marker, stamp, fingerprint, vm):
        written.append((stamp, fingerprint))
        return True

    monkeypatch.setattr("libvirt_backup_system.backup.write_marker", fake_write)

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    captured = capsys.readouterr()
    assert "domain XML changed during inactive backup" in captured.err
    assert written == []


def test_backup_vm_bails_when_required_nfs_mount_disappears(tmp_path: Path, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()

    assert not backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_cleanup_bails_when_required_nfs_mount_disappears(tmp_path: Path, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()

    assert cleanup(cfg) == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_backup_vm_proceeds_when_required_nfs_mount_present(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    monkeypatch.setattr("libvirt_backup_system.cleanup.Path.is_mount", lambda self: True)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert backup_vm(cfg, VM("alpha", "running"), "2026-05", "stamp")


def test_backup_vm_bails_when_mount_disappears_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    # Entry check passes; the recheck right before mkdir flips to False.
    checks = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.cleanup.Path.is_mount", lambda self: next(checks, False))
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_backup_vm_skips_marker_when_post_fingerprint_fails(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    fingerprints = iter(["pre-fp", None])
    monkeypatch.setattr(
        "libvirt_backup_system.backup.domain_xml_fingerprint",
        lambda uri, name: next(fingerprints),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert "inactive fingerprint computation failed" in capsys.readouterr().err


def test_backup_vm_finalize_aborts_when_mount_disappears(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    # Entry + pre-mkdir checks pass; the finalize-time recheck flips to False.
    checks = iter([True, True, False])
    monkeypatch.setattr("libvirt_backup_system.cleanup.Path.is_mount", lambda self: next(checks, False))
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert not backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_confirm_inactive_marker_handles_oserror_on_recheck(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    backup_dir = marker.parent / "oldstamp"
    backup_dir.mkdir()
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    original_is_dir = Path.is_dir
    is_dir_calls = {"n": 0}

    def fake_is_dir(self: Path) -> bool:
        if self == backup_dir:
            is_dir_calls["n"] += 1
            if is_dir_calls["n"] >= 2:
                raise OSError("racey")
            return True
        return original_is_dir(self)

    monkeypatch.setattr("libvirt_backup_system.backup.Path.is_dir", fake_is_dir)
    monkeypatch.setattr("libvirt_backup_system.backup.inactive_marker_is_fresh", lambda uri, name, m: True)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    err = capsys.readouterr().err
    assert "inactive marker backup directory recheck failed" in err


def test_confirm_inactive_marker_recopies_when_backup_dir_disappears(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    backup_dir = marker.parent / "oldstamp"
    backup_dir.mkdir()
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    original_is_dir = Path.is_dir
    removed = {"done": False}

    def vanishing_is_dir(self: Path) -> bool:
        if self == backup_dir and not removed["done"]:
            # First call (inside marked_backup_dir) sees the directory; the
            # second (the TOCTOU recheck) finds it gone.
            removed["done"] = True
            return True
        if self == backup_dir:
            return False
        return original_is_dir(self)

    monkeypatch.setattr("libvirt_backup_system.backup.Path.is_dir", vanishing_is_dir)
    monkeypatch.setattr("libvirt_backup_system.backup.inactive_marker_is_fresh", lambda uri, name, m: True)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert "inactive marker backup directory disappeared" in capsys.readouterr().out
