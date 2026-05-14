from __future__ import annotations

import datetime as dt
from pathlib import Path

from .config import Config, is_month_dir_name
from .inactive_markers import stamp_is_safe
from .logging_json import event
from .paths import backup_root, runtime_backup_path_ok
from .run_records import select_checkpoint
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
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
    if at is None:
        return snapshots[-1][1] if snapshots else None
    # Latest snapshot at-or-before ``at``: walking in reverse stops at the
    # first hit. An ``at`` earlier than every snapshot returns None so the
    # operator gets an explicit error rather than the unrelated latest dir.
    for stamp, chain_dir in reversed(snapshots):
        if stamp <= at:
            return chain_dir
    return None


def _validate_output(output: Path) -> bool:
    if output.exists():
        try:
            entries = list(output.iterdir())
        except (NotADirectoryError, OSError) as exc:
            event("error", "restore output is not a usable directory", output=str(output), error=str(exc))
            return False
        if entries:
            event("error", "restore output is not empty", output=str(output))
            return False
        return True
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        event("error", "restore output directory creation failed", output=str(output), error=str(exc))
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

    Legacy chains without ``runs.jsonl`` fall back to chain-end semantics
    (``--until`` is omitted and the whole chain is replayed). The same fall
    back applies inside chains where the matching record is corrupt or
    missing — restore picks the chain by start time, then stops at the
    matching checkpoint when one is recorded.
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
    if not _validate_output(output):
        return 1
    checkpoint = select_checkpoint(chain_dir, target) if target is not None else None
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
