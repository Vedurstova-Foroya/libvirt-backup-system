from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .config import Config, int_value, iter_month_dirs
from .logging_json import event
from .storage import subpath_is_safe, unsafe_symlink_descendants


def _current_month_name(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def _mount_still_ok(backup_path: Path, where: str, log_path: Path, *, mount_required: bool) -> bool:
    if not mount_required or backup_path.is_mount():
        return True
    event(
        "error",
        f"{where} skipped because BACKUP_PATH is no longer a mount point",
        path=str(log_path),
        backup_path=str(backup_path),
    )
    return False


def _prune_vm_dir(
    vm_dir: Path, keep: int, backup_path: Path, *, mount_required: bool, current_month: str
) -> tuple[int, int]:
    removed = 0
    skipped = 0
    month_dirs = list(iter_month_dirs(vm_dir))
    prunable = month_dirs if keep == 0 else month_dirs[:-keep]
    # Never prune the directory for the current month even if it sorts below
    # other (future-dated) month directories. Clock skew, manual operator
    # mkdir, or a stray ``2099-01/`` directory must not be allowed to push the
    # just-written month out of the keep window — cleanup runs immediately
    # after run_backups and would otherwise delete the data the run just made.
    prunable = [month_dir for month_dir in prunable if month_dir.name != current_month]
    for month_dir in prunable:
        if not subpath_is_safe(backup_path, month_dir):
            event("error", "month cleanup skipped because backup path is unsafe", path=str(month_dir))
            skipped += 1
            continue
        if not _mount_still_ok(backup_path, "month cleanup", month_dir, mount_required=mount_required):
            skipped += 1
            continue
        try:
            shutil.rmtree(month_dir)
        except OSError as exc:
            event("error", "month cleanup failed", path=str(month_dir), error=str(exc))
            skipped += 1
            continue
        removed += 1
        event("info", "removed backup month", path=str(month_dir))
    return removed, skipped


def _prune_month_dirs(
    root: Path, keep: int, backup_path: Path, *, mount_required: bool, current_month: str
) -> tuple[int, int]:
    removed = 0
    skipped = 0
    if keep < 0:
        return removed, skipped
    if not subpath_is_safe(backup_path, root):
        event("error", "cleanup skipped because backup path is unsafe", path=str(root))
        return removed, skipped + 1
    if not root.exists():
        return removed, skipped
    for path in unsafe_symlink_descendants(backup_path, root):
        # Fail closed across the whole tree: a single unsafe symlink could redirect rmtree
        # outside the backup root, so abort cleanup for every VM rather than partially prune.
        event("error", "cleanup skipped because backup tree contains unsafe symlink", path=str(path))
        return removed, skipped + 1
    for vm_dir in sorted(root.iterdir()):
        if not vm_dir.is_dir():
            continue
        if not subpath_is_safe(backup_path, vm_dir):
            event("error", "VM cleanup skipped because backup path is unsafe", path=str(vm_dir))
            skipped += 1
            continue
        vm_removed, vm_skipped = _prune_vm_dir(
            vm_dir, keep, backup_path, mount_required=mount_required, current_month=current_month
        )
        removed += vm_removed
        skipped += vm_skipped
    return removed, skipped


def runtime_backup_path_ok(config: Config) -> bool:
    # Preflight enforces BACKUP_REQUIRE_NFS_MOUNT once at start-of-run. Every
    # filesystem mutation re-checks because the mount can disappear at any time
    # between preflight, mkdir, the backup itself, marker writes, and cleanup.
    if not config.enabled("BACKUP_REQUIRE_NFS_MOUNT"):
        return True
    backup_path = config.path_value("BACKUP_PATH")
    if backup_path.is_mount():
        return True
    event("error", "BACKUP_PATH is no longer a mount point", backup_path=str(backup_path))
    return False


def backup_root(config: Config) -> Path:
    return config.path_value("BACKUP_PATH") / config.get("HOST_ID")


def cleanup(config: Config, *, current_month: str | None = None) -> int:
    if not runtime_backup_path_ok(config):
        return 1
    path = config.path_value("BACKUP_PATH")
    keep = int_value(config.values, "BACKUP_RETENTION_MONTHS")
    # ``current_month`` is the month-name to protect from pruning. The ``run``
    # CLI captures it before run_backups so a month boundary crossed mid-run
    # cannot let a future-dated directory push the just-written month into the
    # prunable set. Standalone ``cleanup`` invocations fall back to "now".
    removed, skipped = _prune_month_dirs(
        backup_root(config),
        keep,
        path,
        mount_required=config.enabled("BACKUP_REQUIRE_NFS_MOUNT"),
        current_month=current_month or _current_month_name(),
    )
    event("info", "cleanup completed", removed_backup_months=removed, skipped=skipped)
    return 0 if skipped == 0 else 1
