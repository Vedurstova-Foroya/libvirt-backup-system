from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from .config import Config, is_month_dir_name
from .logging_json import event
from .paths import backup_root, runtime_backup_path_ok
from .storage import subpath_is_safe
from .vms import is_safe_vm_uuid

# shutil.rmtree's onerror callback signature. The third element is either an
# ``exc_info`` triple (Python <3.12) or a bare exception instance (3.12+); we
# accept the union and format whichever shape arrives.
_RmtreeOnError = Callable[
    [Callable[..., object], str, "tuple[type[BaseException], BaseException, TracebackType] | BaseException"],
    None,
]


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
    return sorted(p for p in root.iterdir() if p.is_dir() and is_safe_vm_uuid(p.name))


def _format_rmtree_error(
    exc_info: tuple[type[BaseException], BaseException, TracebackType] | BaseException,
) -> str:
    # shutil.rmtree's onerror passes ``(exc_type, exc_value, exc_traceback)``
    # on Python <3.12 and the raw exception on 3.12+; coerce both forms into
    # a human-readable string for the log.
    if isinstance(exc_info, BaseException):
        return str(exc_info)
    return str(exc_info[1])


def _log_rmtree_error(vm_uuid: str, month_dir: Path) -> _RmtreeOnError:
    def _on_error(
        _func: Callable[..., object],
        path: str,
        exc_info: tuple[type[BaseException], BaseException, TracebackType] | BaseException,
    ) -> None:
        # shutil.rmtree without an error hook silently leaves a partial tree
        # behind; the next verify run then trips on a half-deleted month dir.
        # Log every per-entry failure so operators can see which files
        # survived and resolve them out-of-band.
        event(
            "error",
            "prune entry failed",
            vm_uuid=vm_uuid,
            month=month_dir.name,
            path=path,
            error=_format_rmtree_error(exc_info),
        )

    return _on_error


def _prune_one_month(config: Config, backup_path: Path, vm_uuid: str, month_dir: Path) -> bool:
    if not subpath_is_safe(backup_path, month_dir):
        event("error", "prune skipped because path is unsafe", vm_uuid=vm_uuid, path=str(month_dir))
        return False
    if not runtime_backup_path_ok(config):
        return False
    try:
        shutil.rmtree(month_dir, onerror=_log_rmtree_error(vm_uuid, month_dir))
    except OSError as exc:
        event("error", "prune failed", vm_uuid=vm_uuid, path=str(month_dir), error=str(exc))
        return False
    if month_dir.exists():
        # onerror swallowed per-entry failures so rmtree itself returned; the
        # month dir is still on disk. Treat that as a prune failure so the run
        # exit code surfaces the incomplete cleanup.
        event("error", "prune left residue", vm_uuid=vm_uuid, path=str(month_dir))
        return False
    event("info", "pruned month", vm_uuid=vm_uuid, month=month_dir.name, path=str(month_dir))
    return True


# virtnbdbackup tags every data file with the run's backup level. ``-l full``
# and ``-l copy`` both produce a standalone, restore-without-deps file
# (``<vm>.<disk>.full.data`` / ``<vm>.<disk>.copy.data`` in real output);
# ``-l inc`` produces ``<vm>.<disk>.inc.virtnbdbackup.<N>.data`` which only
# restores when chained on top of the chain's full. Retention counts a month
# as "has a full backup" iff at least one such standalone file exists, so an
# incremental-only chain (full deleted out-of-band, or a malformed leftover)
# does not satisfy the floor.
_FULL_BACKUP_TOKENS = (".full.", ".copy.")
_BACKUP_DATA_SUFFIX = ".data"


def chain_has_full_backup_file(chain_dir: Path) -> bool:
    try:
        for entry in chain_dir.iterdir():
            if not entry.is_file() or not entry.name.endswith(_BACKUP_DATA_SUFFIX):
                continue
            if any(token in entry.name for token in _FULL_BACKUP_TOKENS):
                return True
    except OSError:
        return False
    return False


def _has_full_backup(month_dir: Path) -> bool:
    """True if the month dir holds at least one chain dir containing a full
    (or ``-l copy``) virtnbdbackup data file.

    A chain dir of pure incrementals does not count: those files cannot be
    restored standalone, so a month with only ``-l inc`` files on disk is not
    a recoverable "month with a backup" for retention purposes. We avoid
    looking at ``<vm>.name`` here because retention sees only the UUID
    directory and the per-VM name may have changed.
    """
    try:
        chains = list(month_dir.iterdir())
    except OSError:
        return False
    for chain in chains:
        if not chain.is_dir():
            continue
        if chain_has_full_backup_file(chain):
            return True
    return False


def _prune_vm(
    config: Config,
    backup_path: Path,
    vm_dir: Path,
    keep: int,
    *,
    current_month: str | None = None,
) -> bool:
    month_dirs = sorted(
        (p for p in vm_dir.iterdir() if p.is_dir() and is_month_dir_name(p.name)),
        key=lambda p: _month_sort_key(p.name),
    )
    if len(month_dirs) <= keep:
        return True
    # Both gates only apply when the orchestrated ``run`` path threads the
    # current calendar month through. Test/verify helpers calling
    # ``prune_old_months`` with ``current_month=None`` get the bare
    # count-based behavior so unit fixtures don't have to seed real backup
    # data inside every month dir.
    if current_month is not None:
        current_dir = next((p for p in month_dirs if p.name == current_month), None)
        if current_dir is None or not _has_full_backup(current_dir):
            # Gate 1: hold pruning until this month has its own full (or
            # ``-l copy``) backup on disk. Pure incrementals on top of an
            # earlier chain cannot satisfy this — the month must own a
            # restore-standalone file.
            event(
                "info",
                "retention skipped for VM without full backup in current month",
                vm_uuid=vm_dir.name,
                current_month=current_month,
            )
            return True
        # Gate 2: only prune once at least ``keep`` months *each* contain a
        # full backup. With retention=12 this enforces "12 months with at
        # least one full each" before the oldest is dropped — a chain that
        # collected only increments, or a leftover empty month dir, will
        # not raise the count toward the floor.
        full_month_dirs = [p for p in month_dirs if _has_full_backup(p)]
        months_with_full = len(full_month_dirs)
        if months_with_full <= keep:
            event(
                "info",
                "retention skipped because months-with-full-backup count is at or below floor",
                vm_uuid=vm_dir.name,
                months_with_full=months_with_full,
                required_floor=keep + 1,
            )
            return True
        to_delete = full_month_dirs[: months_with_full - keep]
    else:
        to_delete = month_dirs[: len(month_dirs) - keep]
    # Defensive: never drop the most recent month even if it somehow ends up in
    # the to-delete slice. Single-month VMs hit this branch too: if keep == 0
    # the slice is the whole list, but we still keep the newest dir so an
    # accidental retention=0 misconfiguration cannot wipe every backup at once.
    most_recent = month_dirs[-1]
    ok = True
    for month_dir in to_delete:
        if month_dir == most_recent:
            continue
        if not _prune_one_month(config, backup_path, vm_dir.name, month_dir):
            ok = False
    return ok


def prune_old_months(config: Config, *, current_month: str | None = None) -> int:
    """Drop month dirs older than ``BACKUP_RETENTION_MONTHS`` for every VM.

    Returns ``0`` on success, ``1`` if any prune step failed. ``0`` is also
    returned when ``BACKUP_RETENTION_MONTHS=0`` (pruning disabled) so a
    misconfiguration cannot fail the run.

    When ``current_month`` is supplied the prune for a VM is gated on two
    safety checks: the current month must hold its own full (or
    ``-l copy``) backup file, AND at least ``BACKUP_RETENTION_MONTHS`` other
    calendar months must also each hold a full of their own. Pure
    incremental chain dirs (or leftover empty month dirs) do not satisfy
    either gate. Together these turn retention=N into "only delete the
    oldest month once N+1 months *each* hold a restore-standalone full",
    so a missed or partially failed run never shrinks the window below
    the configured floor. Callers that pass ``None`` (verify/test helpers)
    prune purely by ``BACKUP_RETENTION_MONTHS`` without the full-backup
    gates.
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
        if not _prune_vm(config, backup_path, vm_dir, keep, current_month=current_month):
            ok = False
    return 0 if ok else 1
