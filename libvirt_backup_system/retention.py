from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config, is_month_dir_name
from .logging_json import event
from .paths import backup_root, runtime_backup_path_ok
from .storage import subpath_is_safe


def _retention_months(config: Config) -> int | None:
    raw = config.get("BACKUP_RETENTION_MONTHS")
    try:
        months = int(raw)
    except ValueError:
        event("error", "BACKUP_RETENTION_MONTHS is not an integer", value=raw)
        return None
    if months < 0:
        event("error", "BACKUP_RETENTION_MONTHS must be >= 0", value=raw)
        return None
    return months


def _month_sort_key(name: str) -> tuple[int, int]:
    # Pre-validated by ``is_month_dir_name``; the helper still ints the parts so
    # ordering by tuple stays correct at year rollovers (zero-padded YYYY-MM
    # already sorts lex-correctly, but the tuple form makes the intent explicit).
    return int(name[:4]), int(name[5:])


def _iter_vm_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir())


def _prune_one_month(config: Config, backup_path: Path, vm_uuid: str, month_dir: Path) -> bool:
    if not subpath_is_safe(backup_path, month_dir):
        event("error", "prune skipped because path is unsafe", vm_uuid=vm_uuid, path=str(month_dir))
        return False
    if not runtime_backup_path_ok(config):
        return False
    try:
        shutil.rmtree(month_dir)
    except OSError as exc:
        event("error", "prune failed", vm_uuid=vm_uuid, path=str(month_dir), error=str(exc))
        return False
    event("info", "pruned month", vm_uuid=vm_uuid, month=month_dir.name, path=str(month_dir))
    return True


def _prune_vm(config: Config, backup_path: Path, vm_dir: Path, keep: int) -> bool:
    month_dirs = sorted(
        (p for p in vm_dir.iterdir() if p.is_dir() and is_month_dir_name(p.name)),
        key=lambda p: _month_sort_key(p.name),
    )
    if len(month_dirs) <= keep:
        return True
    # Defensive: never drop the most recent month even if it somehow ends up in
    # the to-delete slice. Single-month VMs hit this branch too: if keep == 0
    # the slice is the whole list, but we still keep the newest dir so an
    # accidental retention=0 misconfiguration cannot wipe every backup at once.
    to_delete = month_dirs[: len(month_dirs) - keep]
    most_recent = month_dirs[-1]
    ok = True
    for month_dir in to_delete:
        if month_dir == most_recent:
            continue
        if not _prune_one_month(config, backup_path, vm_dir.name, month_dir):
            ok = False
    return ok


def prune_old_months(config: Config) -> int:
    """Drop month dirs older than ``BACKUP_RETENTION_MONTHS`` for every VM.

    Returns ``0`` on success, ``1`` if any prune step failed. ``0`` is also
    returned when ``BACKUP_RETENTION_MONTHS=0`` (pruning disabled) so a
    misconfiguration cannot fail the run.
    """
    keep = _retention_months(config)
    if keep is None:
        return 1
    if keep == 0:
        event("info", "retention disabled", reason="BACKUP_RETENTION_MONTHS=0")
        return 0
    if not runtime_backup_path_ok(config):
        return 1
    backup_path = config.path_value("BACKUP_PATH")
    root = backup_root(config)
    if not subpath_is_safe(backup_path, root):
        event("error", "retention skipped because backup root is unsafe", path=str(root))
        return 1
    ok = True
    for vm_dir in _iter_vm_dirs(root):
        if not subpath_is_safe(backup_path, vm_dir):
            event("error", "retention skipped because VM path is unsafe", path=str(vm_dir))
            ok = False
            continue
        if not _prune_vm(config, backup_path, vm_dir, keep):
            ok = False
    return 0 if ok else 1
