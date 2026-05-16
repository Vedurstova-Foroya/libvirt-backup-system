from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.retention import prune_old_months
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


def _seed(cfg: Config, vm_uuid: str, months: list[str]) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / vm_uuid
    vm_dir.mkdir(parents=True, exist_ok=True)
    for month in months:
        (vm_dir / month).mkdir()
    return vm_dir


def _enable_pruning(cfg: Config, months: int) -> Config:
    cfg.values["BACKUP_RETENTION_MONTHS"] = str(months)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_prune_old_months_disabled_when_zero(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _enable_pruning(backup_config, 0)
    _seed(cfg, ALPHA_UUID, ["2025-01", "2026-01"])
    assert prune_old_months(cfg) == 0
    out = capsys.readouterr().out
    assert "retention disabled" in out
    # Nothing was deleted.
    for month in ("2025-01", "2026-01"):
        assert (cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / month).is_dir()


def test_prune_old_months_invalid_value(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _enable_pruning(backup_config, 2)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "abc"
    assert prune_old_months(cfg) == 1
    assert "is not an integer" in capsys.readouterr().err


def test_prune_old_months_negative_value(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _enable_pruning(backup_config, 2)
    cfg.values["BACKUP_RETENTION_MONTHS"] = "-1"
    assert prune_old_months(cfg) == 1
    assert "must be >= 0" in capsys.readouterr().err


def test_prune_old_months_keeps_recent_drops_old(tmp_path: Path, backup_config: Config) -> None:
    cfg = _enable_pruning(backup_config, 2)
    vm_dir = _seed(cfg, ALPHA_UUID, ["2025-10", "2025-11", "2025-12", "2026-01"])
    # Add a non-month file/dir under the VM dir; retention must ignore it.
    (vm_dir / "scratch").mkdir()
    assert prune_old_months(cfg) == 0
    remaining = sorted(p.name for p in vm_dir.iterdir() if p.is_dir())
    # Keeps the 2 most recent month dirs + the foreign "scratch" dir.
    assert remaining == ["2025-12", "2026-01", "scratch"]


def test_prune_old_months_year_boundary_sort(tmp_path: Path, backup_config: Config) -> None:
    cfg = _enable_pruning(backup_config, 1)
    vm_dir = _seed(cfg, ALPHA_UUID, ["2025-12", "2026-01"])
    # Calendar months sort cleanly across the year boundary: (2025, 12) < (2026,
    # 01) under tuple comparison, so 2025-12 drops and 2026-01 is kept.
    assert prune_old_months(cfg) == 0
    remaining = sorted(p.name for p in vm_dir.iterdir())
    assert remaining == ["2026-01"]


def test_prune_old_months_never_drops_single_month(tmp_path: Path, backup_config: Config) -> None:
    # Defensive: a VM with exactly one month of backups must never be wiped
    # even if retention=0 leaked through somehow. With retention=1 and a single
    # month present, the keep slice covers everything, so no delete occurs.
    cfg = _enable_pruning(backup_config, 1)
    vm_dir = _seed(cfg, ALPHA_UUID, ["2026-05"])
    assert prune_old_months(cfg) == 0
    assert (vm_dir / "2026-05").is_dir()


def test_prune_old_months_skips_unsafe_vm_dir(tmp_path: Path, backup_config: Config, monkeypatch, capsys) -> None:
    cfg = _enable_pruning(backup_config, 1)
    _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])
    backup_path = cfg.path_value("BACKUP_PATH")
    real = backup_path / cfg.get("HOST_ID") / ALPHA_UUID
    monkeypatch.setattr(
        "libvirt_backup_system.retention.subpath_is_safe",
        lambda root, path: path != real,
    )
    assert prune_old_months(cfg) == 1
    assert "retention skipped because VM path is unsafe" in capsys.readouterr().err


def test_prune_old_months_unsafe_root(tmp_path: Path, backup_config: Config, monkeypatch, capsys) -> None:
    cfg = _enable_pruning(backup_config, 1)
    _seed(cfg, ALPHA_UUID, ["2025-10"])
    monkeypatch.setattr("libvirt_backup_system.retention.subpath_is_safe", lambda root, path: False)
    assert prune_old_months(cfg) == 1
    assert "retention skipped because backup root is unsafe" in capsys.readouterr().err


def test_prune_old_months_rmtree_failure(tmp_path: Path, backup_config: Config, monkeypatch, capsys) -> None:
    cfg = _enable_pruning(backup_config, 1)
    _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])

    def fail(path: Path, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.retention.shutil.rmtree", fail)
    assert prune_old_months(cfg) == 1
    assert "prune failed" in capsys.readouterr().err


def test_format_rmtree_error_handles_both_callback_shapes() -> None:
    # shutil.rmtree's onerror signature changed at Python 3.12: pre-3.12 it
    # passes ``(exc_type, exc_value, exc_traceback)``; 3.12+ passes the bare
    # exception. Both must format to the same human-readable string.
    from libvirt_backup_system.retention import _format_rmtree_error

    err = OSError("EACCES")
    assert _format_rmtree_error(err) == "EACCES"
    assert _format_rmtree_error((type(err), err, None)) == "EACCES"


def test_prune_old_months_rmtree_partial_left_residue(
    tmp_path: Path, backup_config: Config, monkeypatch, capsys
) -> None:
    # Per-entry onerror swallows the OSError so rmtree returns normally even
    # though the month dir is still on disk. Retention must then catch the
    # residue and surface a failure exit code so operators see the incomplete
    # cleanup. Without this, an ACL-blocked subentry would silently turn
    # retention into "tried but did nothing useful".
    cfg = _enable_pruning(backup_config, 1)
    _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID

    def partial_rmtree(path: Path, **kwargs: object) -> None:
        onerror = kwargs.get("onerror")
        if callable(onerror):
            # Python <3.12 passes ``(exc_type, exc_value, exc_traceback)``
            # to onerror; pass the tuple here to exercise that branch of
            # _format_rmtree_error and the per-entry log path.
            err = OSError("EACCES")
            onerror(None, str(path / "stuck"), (type(err), err, None))
        # Month dir intentionally left in place to simulate the post-onerror
        # state. The retention layer must catch the survivor and fail.

    monkeypatch.setattr("libvirt_backup_system.retention.shutil.rmtree", partial_rmtree)
    assert prune_old_months(cfg) == 1
    err = capsys.readouterr().err
    assert "prune entry failed" in err
    assert "prune left residue" in err
    # Both month dirs survive because onerror left them on disk.
    assert (vm_dir / "2025-10").is_dir()


def test_prune_old_months_mount_required_but_missing(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _enable_pruning(backup_config, 1)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])
    # Backup path was created but is not a mount point in tests; the runtime
    # check refuses to run retention against a dropped NFS mount.
    assert prune_old_months(cfg) == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_prune_old_months_handles_two_vms(tmp_path: Path, backup_config: Config) -> None:
    cfg = _enable_pruning(backup_config, 1)
    alpha_dir = _seed(cfg, ALPHA_UUID, ["2025-01", "2026-01"])
    beta_dir = _seed(cfg, BETA_UUID, ["2025-10", "2026-02"])
    assert prune_old_months(cfg) == 0
    assert sorted(p.name for p in alpha_dir.iterdir()) == ["2026-01"]
    assert sorted(p.name for p in beta_dir.iterdir()) == ["2026-02"]


def test_prune_old_months_skips_unsafe_month_dir(tmp_path: Path, backup_config: Config, monkeypatch, capsys) -> None:
    cfg = _enable_pruning(backup_config, 1)
    vm_dir = _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])
    target_month = vm_dir / "2025-10"
    monkeypatch.setattr(
        "libvirt_backup_system.retention.subpath_is_safe",
        lambda root, path: path != target_month,
    )
    assert prune_old_months(cfg) == 1
    assert "prune skipped because path is unsafe" in capsys.readouterr().err


def test_prune_old_months_no_backup_root(tmp_path: Path, backup_config: Config) -> None:
    # Empty backup tree must not be treated as an error.
    cfg = _enable_pruning(backup_config, 1)
    cfg.path_value("BACKUP_PATH").mkdir(exist_ok=True)
    assert prune_old_months(cfg) == 0


def test_prune_one_month_rechecks_mount_after_safety(
    tmp_path: Path,
    backup_config: Config,
    monkeypatch,
    capsys,
) -> None:
    # After the per-month subpath check passes, an NFS drop between safety
    # check and rmtree must abort the delete. This is the inverse of the
    # earlier "mount required but missing at entry" test: here the entry-time
    # check passes and the per-delete recheck fails.
    cfg = _enable_pruning(backup_config, 1)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    _seed(cfg, ALPHA_UUID, ["2025-10", "2026-01"])
    mount_calls = iter([True, False])
    monkeypatch.setattr(
        "libvirt_backup_system.paths.Path.is_mount",
        lambda self: next(mount_calls, False),
    )
    assert prune_old_months(cfg) == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_prune_old_months_keep_zero_preserves_most_recent(tmp_path: Path, backup_config: Config) -> None:
    # Defensive guard: keep=0 normally is the "disabled" path (handled by
    # _retention_months), but if someone bypasses _retention_months the helper
    # still refuses to drop the newest month. Force the path via direct call
    # so the most_recent==month_dir branch is exercised.
    from libvirt_backup_system.retention import _prune_vm

    cfg = _enable_pruning(backup_config, 1)
    vm_dir = _seed(cfg, ALPHA_UUID, ["2025-01", "2025-02"])
    backup_path = cfg.path_value("BACKUP_PATH")
    # With keep=0 the slice is the whole list; the most-recent guard then
    # protects 2025-02 from deletion. 2025-01 is older and will be pruned.
    assert _prune_vm(cfg, backup_path, vm_dir, 0)
    remaining = sorted(p.name for p in vm_dir.iterdir())
    assert remaining == ["2025-02"]
