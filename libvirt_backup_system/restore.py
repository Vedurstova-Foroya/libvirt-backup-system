from __future__ import annotations

import datetime as dt
import stat
from contextlib import suppress
from pathlib import Path

from .config import Config, is_month_dir_name
from .inactive_markers import stamp_is_safe
from .logging_json import event
from .paths import backup_root, runtime_backup_path_ok
from .retention import chain_has_full_backup_file
from .run_records import SelectStatus, chain_is_poisoned, select_checkpoint
from .shell import CommandError, run_streamed
from .storage import resolved_path_is_within, subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid, resolve_vm_uuid

# Chain dirs are named with backup.timestamp() in UTC, e.g. ``20260507T101112``.
# strptime is the chain dir's authoritative parser: stamp_is_safe filters dot
# files and traversal, but ``20260507T999999`` would pass it while being an
# invalid timestamp; strptime rejects that here so the snapshot list only ever
# contains chains with a real start time.
STAMP_FORMAT = "%Y%m%dT%H%M%S"


def _resolve_vm_root(config: Config, root: Path, name_or_uuid: str) -> Path | None:
    if not (is_safe_vm_name(name_or_uuid) or is_safe_vm_uuid(name_or_uuid)):
        event("error", "restore target name is invalid", vm=name_or_uuid)
        return None
    candidate = root / name_or_uuid
    if candidate.is_dir():
        return candidate
    uuid = resolve_vm_uuid(config, name_or_uuid)
    if uuid is None:
        event("error", "restore target not found", vm=name_or_uuid)
        return None
    resolved = root / uuid
    if resolved.is_dir():
        return resolved
    event("error", "restore target not found", vm=name_or_uuid, uuid=uuid, path=str(resolved))
    return None


def _parse_chain_stamp(name: str) -> dt.datetime | None:
    if not stamp_is_safe(name):
        return None
    try:
        return dt.datetime.strptime(name, STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def parse_at(value: str) -> dt.datetime | None:
    """Parse ``--at`` into a UTC datetime, or ``None`` when malformed.

    Accepts the chain dir's compact ``YYYYMMDDTHHMMSS`` form and anything
    ``datetime.fromisoformat`` handles: ``YYYY-MM-DD``, ``YYYY-MM-DDTHH:MM:SS``,
    and the same with a space separator or trailing offset. Naive values are
    interpreted as UTC because the chain dir names themselves are UTC.
    """
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(candidate, STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _enumerate_snapshots(vm_root: Path) -> list[tuple[dt.datetime, Path]]:
    """Return ``(start_time, chain_dir)`` for every parseable chain under vm_root.

    Walks month dirs in name order and chain dirs by directory listing; the
    caller sorts by ``start_time`` so callers never depend on any particular
    enumeration order.
    """
    snapshots: list[tuple[dt.datetime, Path]] = []
    for month_dir in sorted(vm_root.iterdir(), key=lambda p: p.name):
        if not month_dir.is_dir() or not is_month_dir_name(month_dir.name):
            continue
        for chain_dir in month_dir.iterdir():
            if not chain_dir.is_dir():
                continue
            stamp = _parse_chain_stamp(chain_dir.name)
            if stamp is None:
                continue
            snapshots.append((stamp, chain_dir))
    snapshots.sort(key=lambda item: item[0])
    return snapshots


def _pick_snapshot(snapshots: list[tuple[dt.datetime, Path]], at: dt.datetime | None) -> Path | None:
    restorable = [(stamp, chain_dir) for stamp, chain_dir in snapshots if chain_has_full_backup_file(chain_dir)]
    if at is None:
        return restorable[-1][1] if restorable else None
    # Latest snapshot at-or-before ``at``: walking in reverse stops at the
    # first hit. An ``at`` earlier than every snapshot returns None so the
    # operator gets an explicit error rather than the unrelated latest dir.
    for stamp, chain_dir in reversed(restorable):
        if stamp <= at:
            return chain_dir
    return None


def _validate_output(output: Path, backup_path: Path) -> bool:
    # Reject symlinks before anything else: ``output.exists()`` follows the
    # link, so an attacker-controlled symlink pointing at an empty directory
    # would otherwise pass the "exists + empty" guard and let virtnbdrestore
    # write through the symlink to a path of the attacker's choosing. ``lstat``
    # does not follow, so a dangling symlink also fails here.
    try:
        link_st = output.lstat()
    except FileNotFoundError:
        link_st = None
    except OSError as exc:
        event("error", "restore output stat failed", output=str(output), error=str(exc))
        return False
    if link_st is not None and stat.S_ISLNK(link_st.st_mode):
        event("error", "restore output is a symlink", output=str(output))
        return False
    try:
        output_is_in_backup_path = resolved_path_is_within(backup_path, output)
    except (OSError, RuntimeError) as exc:
        event("error", "restore output path resolution failed", output=str(output), error=str(exc))
        return False
    if output_is_in_backup_path:
        event("error", "restore output is inside BACKUP_PATH", output=str(output), backup_path=str(backup_path))
        return False
    if link_st is not None:
        if not stat.S_ISDIR(link_st.st_mode):
            event("error", "restore output is not a directory", output=str(output))
            return False
        try:
            entries = list(output.iterdir())
        except OSError as exc:
            event("error", "restore output is not a usable directory", output=str(output), error=str(exc))
            return False
        if entries:
            event("error", "restore output is not empty", output=str(output))
            return False
        return True
    try:
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError as exc:
        event("error", "restore output directory creation failed", output=str(output), error=str(exc))
        return False
    # Re-verify after mkdir: a symlink swap on an intermediate parent between
    # the initial lstat/resolve and mkdir(parents=True) could land the new
    # directory inside BACKUP_PATH.
    try:
        post_inside = resolved_path_is_within(backup_path, output)
    except (OSError, RuntimeError):
        post_inside = True
    if post_inside:
        with suppress(OSError):
            output.rmdir()
        event("error", "restore output resolved inside BACKUP_PATH after mkdir", output=str(output))
        return False
    return True


def restore(
    config: Config,
    vm_name_or_uuid: str,
    output: Path,
    *,
    at: str | None = None,
) -> int:
    """Restore the run whose recorded time is at-or-before ``at`` (or the latest).

    ``--at`` resolves at per-run (checkpoint) granularity for chains backed up
    by this version: each successful run writes ``runs.jsonl`` mapping the
    run timestamp to the new virtnbdbackup checkpoint, and restore passes
    ``virtnbdrestore --until <checkpoint>`` to stop exactly at that run.

    Chains predating this feature ship with no ``runs.jsonl`` at all and
    legitimately fall back to chain-end semantics (``--until`` is omitted and
    the whole chain is replayed). Chains where ``runs.jsonl`` exists but is
    unusable (truncated, hand-edited into invalid JSON, or with every record
    in the future of ``at``) are refused: silently falling back to chain end
    would restore a newer state than the operator asked for, and the safer
    answer is an explicit failure that the operator can resolve manually.

    ``VM_BLACKLIST`` is intentionally ignored here: a VM that has been
    blacklisted today may still have valid backups taken before it was added,
    and the operator must be able to recover from them. The blacklist scopes
    to *taking* backups, not to restoring them.
    """
    if not runtime_backup_path_ok(config):
        return 1
    target: dt.datetime | None = None
    if at is not None:
        target = parse_at(at)
        if target is None:
            event("error", "restore --at is malformed", at=at)
            return 1
    backup_path = config.path_value("BACKUP_PATH")
    root = backup_root(config)
    if not subpath_is_safe(backup_path, root):
        event("error", "restore skipped because backup root is unsafe", path=str(root))
        return 1
    vm_root = _resolve_vm_root(config, root, vm_name_or_uuid)
    if vm_root is None or not subpath_is_safe(backup_path, vm_root):
        if vm_root is not None:
            event("error", "restore skipped because VM root is unsafe", path=str(vm_root))
        return 1
    snapshots = _enumerate_snapshots(vm_root)
    if not snapshots:
        event("error", "restore found no backups", vm_root=str(vm_root))
        return 1
    chain_dir = _pick_snapshot(snapshots, target)
    if chain_dir is None:
        event("error", "restore --at is earlier than the oldest backup", at=at, oldest=snapshots[0][1].name)
        return 1
    if not subpath_is_safe(backup_path, chain_dir):
        event("error", "restore skipped because chain path is unsafe", path=str(chain_dir))
        return 1
    if not _validate_output(output, backup_path):
        return 1
    checkpoint: str | None = None
    if target is None:
        if chain_is_poisoned(chain_dir):
            event(
                "error",
                "restore refused poisoned chain end",
                chain_dir=str(chain_dir),
            )
            return 1
    else:
        selection = select_checkpoint(chain_dir, target)
        if selection.status is SelectStatus.MISSING:
            # ``runs.jsonl`` is present but has no record at-or-before ``at``
            # (truncated first record, or every record is in the future).
            # Falling back to chain end would silently restore a newer state
            # than the operator asked for — refuse instead.
            event(
                "error",
                "restore --at has no matching run record",
                at=at,
                chain_dir=str(chain_dir),
            )
            return 1
        if selection.status is SelectStatus.POISONED:
            event(
                "error",
                "restore --at would replay poisoned chain end",
                at=at,
                chain_dir=str(chain_dir),
            )
            return 1
        checkpoint = selection.checkpoint
    cmd = ["virtnbdrestore", "-a", "restore", "-i", str(chain_dir), "-o", str(output)]
    if checkpoint is not None:
        cmd.extend(["-u", checkpoint])
    event("info", "restore started", source=str(chain_dir), output=str(output), checkpoint=checkpoint or "")
    try:
        run_streamed(cmd)
    except CommandError as exc:
        event("error", "restore failed", stderr=exc.result.stderr.strip(), returncode=exc.result.returncode)
        return 1
    except OSError as exc:
        # FileNotFoundError / PermissionError from Popen — virtnbdrestore is
        # missing on this host. Surface it as a clean operator error rather
        # than the cli's generic fatal-traceback path so a recovery-host
        # operator who skipped ``check`` gets a useful message.
        event("error", "restore failed: virtnbdrestore unavailable", error=str(exc))
        return 1
    event("info", "restore completed", source=str(chain_dir), output=str(output))
    return 0
