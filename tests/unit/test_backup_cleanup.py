from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.backup import cleanup
from libvirt_backup_system.config import Config

from .conftest import symlink_backup_vm_path


def _with_retention(cfg: Config) -> Config:
    cfg.values["BACKUP_RETENTION_MONTHS"] = "2"
    return cfg


def test_cleanup_backup_tree(tmp_path: Path, capsys, backup_config) -> None:
    cfg = _with_retention(backup_config)
    for month in ["2026-01", "2026-02", "2026-03", "bad"]:
        (tmp_path / "backups/host/alpha" / month).mkdir(parents=True)
    (tmp_path / "backups/host/README").write_text("ignored", encoding="utf-8")

    assert cleanup(cfg) == 0
    assert not (tmp_path / "backups/host/alpha/2026-01").exists()
    assert (tmp_path / "backups/host/alpha/2026-02").exists()
    assert (tmp_path / "backups/host/alpha/2026-03").exists()

    completion_event = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if json.loads(line).get("message") == "cleanup completed"
    )
    assert completion_event["removed_backup_months"] == 1


@pytest.mark.parametrize("symlink_case", ["host", "vm"])
def test_cleanup_skips_symlinked_backup_subpaths_without_pruning_targets(
    tmp_path: Path,
    capsys,
    symlink_case: str,
    backup_config,
) -> None:
    cfg = _with_retention(backup_config)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    (tmp_path / "backups").mkdir()
    outside_vm = symlink_backup_vm_path(tmp_path, symlink_case)

    outside_data = outside_vm / "2026-01/data"
    outside_data.parent.mkdir(parents=True)
    outside_data.write_text("keep me", encoding="utf-8")

    assert cleanup(cfg) == 1
    assert outside_data.read_text(encoding="utf-8") == "keep me"
    assert "unsafe" in capsys.readouterr().err


def test_cleanup_skips_symlinked_backup_month_without_pruning_target(
    tmp_path: Path,
    capsys,
    backup_config,
) -> None:
    cfg = _with_retention(backup_config)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    outside_data = tmp_path / "outside/month/data"
    outside_data.parent.mkdir(parents=True)
    outside_data.write_text("keep me", encoding="utf-8")
    backup_vm = tmp_path / "backups/host/alpha"
    backup_vm.mkdir(parents=True)
    (backup_vm / "2026-01").symlink_to(outside_data.parent, target_is_directory=True)

    assert cleanup(cfg) == 1
    assert outside_data.read_text(encoding="utf-8") == "keep me"
    assert "cleanup skipped because backup tree contains unsafe symlink" in capsys.readouterr().err


def test_cleanup_keeps_all_when_retention_is_minus_one(tmp_path: Path, backup_config) -> None:
    cfg = _with_retention(backup_config)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "-1"
    for month in ["2026-01", "2026-02", "2026-03"]:
        (tmp_path / "backups/host/alpha" / month).mkdir(parents=True)
    assert cleanup(cfg) == 0
    for month in ["2026-01", "2026-02", "2026-03"]:
        assert (tmp_path / "backups/host/alpha" / month).exists()


def test_cleanup_missing_root_and_unsafe_descendants(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _with_retention(backup_config)
    assert cleanup(cfg) == 0

    vm_dir = tmp_path / "backups/host/alpha"
    month_dir = vm_dir / "2026-01"
    month_dir.mkdir(parents=True)
    checks = iter([True, False, True, True, False])
    monkeypatch.setattr("libvirt_backup_system.cleanup.subpath_is_safe", lambda root, path: next(checks))
    assert cleanup(cfg) == 1
    assert month_dir.exists()
    assert "VM cleanup skipped because backup path is unsafe" in capsys.readouterr().err

    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    checks = iter([True, True, False])
    assert cleanup(cfg) == 1
    assert month_dir.exists()
    assert "month cleanup skipped because backup path is unsafe" in capsys.readouterr().err


def test_cleanup_returns_nonzero_when_root_path_unsafe(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _with_retention(backup_config)
    (tmp_path / "backups/host").mkdir(parents=True)
    monkeypatch.setattr("libvirt_backup_system.cleanup.subpath_is_safe", lambda root, path: False)

    assert cleanup(cfg) == 1
    assert "cleanup skipped because backup path is unsafe" in capsys.readouterr().err


def test_cleanup_returns_nonzero_when_rmtree_fails(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _with_retention(backup_config)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    month_dir = tmp_path / "backups/host/alpha/2026-01"
    month_dir.mkdir(parents=True)

    def fake_rmtree(path: Path) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr("libvirt_backup_system.cleanup.shutil.rmtree", fake_rmtree)
    assert cleanup(cfg) == 1
    assert month_dir.exists()
    assert "month cleanup failed" in capsys.readouterr().err


def test_cleanup_skips_pruning_when_mount_disappears_between_initial_check_and_rmtree(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _with_retention(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.values["BACKUP_RETENTION_MONTHS"] = "0"
    month_dir = tmp_path / "backups/host/alpha/2026-01"
    month_dir.mkdir(parents=True)
    # First is_mount call gates cleanup entry (must be True); the per-month
    # recheck inside _prune_vm_dir flips to False so rmtree is skipped.
    states = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.cleanup.Path.is_mount", lambda self: next(states, False))

    assert cleanup(cfg) == 1
    assert month_dir.exists()
    err = capsys.readouterr().err
    assert "month cleanup skipped because BACKUP_PATH is no longer a mount point" in err
