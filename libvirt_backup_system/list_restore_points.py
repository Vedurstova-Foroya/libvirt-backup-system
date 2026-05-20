"""Enumerate every restorable backup point across all hosts and VMs.

``list-restore-points`` walks ``BACKUP_PATH/<host>/<vm-uuid>/<yyyy-mm>/<chain>/`` for
every host directory present under ``BACKUP_PATH`` — not just the current
``HOST_ID`` — because a recovery host needs to see and restore from backups
that were taken on a different KVM host. Each chain emits one row per
``runs.jsonl`` record, or a single chain-end row for legacy chains predating
that file. The first two columns are the VM UUID and the per-run timestamp so
operators can copy them straight from the listing into ``restore``.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import stamp_is_safe
from .config import Config, is_month_dir_name
from .logging_json import event
from .paths import runtime_backup_path_ok
from .run_records import RUNS_FILE
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid

STAMP_FORMAT = "%Y%m%dT%H%M%S"


@dataclass(frozen=True)
class BackupRow:
    vm_uuid: str
    timestamp: str
    host_id: str
    vm_name: str
    kind: str
    month: str
    chain_id: str
    chain_dir: Path
    checkpoint: str | None  # None for legacy chains restored at chain-end


def _parse_chain_stamp(name: str) -> dt.datetime | None:
    if not stamp_is_safe(name):
        return None
    try:
        return dt.datetime.strptime(name, STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _read_vm_name(chain_dir: Path) -> str:
    """Return the VM name virtnbdbackup recorded in this chain, or "".

    virtnbdbackup writes a ``<vm_name>.cpt`` checkpoint state file alongside
    its data files; we already depend on that filename in ``run_records``.
    The stem is the most reliable source of the VM name because virtnbdbackup
    derives it from the domain at backup time. Empty string when no ``.cpt``
    is present or the recorded name fails safety validation.
    """
    try:
        entries = list(chain_dir.iterdir())
    except OSError:
        return ""
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix != ".cpt":
                continue
        except OSError:
            continue
        name = entry.stem
        if is_safe_vm_name(name):
            return name
    return ""


def _chain_kind(timestamp: str, chain_id: str) -> str:
    """Classify the per-run backup level: full or inc.

    Running-VM chains have one full + N incrementals; the first record
    (``timestamp == chain_id``) is the full, the rest are incrementals.
    """
    return "full" if timestamp == chain_id else "inc"


def _read_runs(chain_dir: Path) -> list[tuple[str, str]]:
    """Return ``(ts, checkpoint)`` records from ``runs.jsonl`` in chain order.

    Malformed lines and missing files yield an empty list so callers fall back
    to the legacy chain-end form. ``list-restore-points`` is a read-only display, so
    a corrupt record is better surfaced as "no runs visible for this chain"
    than as a crash that hides every other chain on the host.
    """
    try:
        raw = (chain_dir / RUNS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[tuple[str, str]] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            ts = data["ts"]
            checkpoint = data["checkpoint"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(ts, str) and isinstance(checkpoint, str) and ts and checkpoint:
            records.append((ts, checkpoint))
    return records


def _iter_chain_dirs(vm_dir: Path) -> list[tuple[str, str, Path]]:
    """Yield ``(month, chain_id, chain_dir)`` for every chain under ``vm_dir``."""
    out: list[tuple[str, str, Path]] = []
    try:
        month_entries = sorted(vm_dir.iterdir(), key=lambda p: p.name)
    except OSError:
        return out
    for month_dir in month_entries:
        if not month_dir.is_dir() or not is_month_dir_name(month_dir.name):
            continue
        try:
            chain_entries = sorted(month_dir.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for chain_dir in chain_entries:
            if not chain_dir.is_dir() or _parse_chain_stamp(chain_dir.name) is None:
                continue
            out.append((month_dir.name, chain_dir.name, chain_dir))
    return out


def _iter_vm_dirs_for_host(host_dir: Path, vm_uuid: str | None) -> list[Path]:
    if vm_uuid is not None:
        candidate = host_dir / vm_uuid
        return [candidate] if candidate.is_dir() else []
    try:
        return sorted((p for p in host_dir.iterdir() if p.is_dir() and is_safe_vm_uuid(p.name)), key=lambda p: p.name)
    except OSError:
        return []


def _iter_host_dirs(backup_path: Path) -> list[Path]:
    try:
        return sorted((p for p in backup_path.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []


def _rows_for_chain(host_id: str, vm_uuid: str, month: str, chain_id: str, chain_dir: Path) -> list[BackupRow]:
    vm_name = _read_vm_name(chain_dir)
    runs = _read_runs(chain_dir)
    if runs:
        return [
            BackupRow(
                vm_uuid=vm_uuid,
                timestamp=ts,
                host_id=host_id,
                vm_name=vm_name,
                kind=_chain_kind(ts, chain_id),
                month=month,
                chain_id=chain_id,
                chain_dir=chain_dir,
                checkpoint=checkpoint,
            )
            for ts, checkpoint in runs
        ]
    return [
        BackupRow(
            vm_uuid=vm_uuid,
            timestamp=chain_id,
            host_id=host_id,
            vm_name=vm_name,
            kind=_chain_kind(chain_id, chain_id),
            month=month,
            chain_id=chain_id,
            chain_dir=chain_dir,
            checkpoint=None,
        )
    ]


def enumerate_backups(config: Config, *, vm_uuid: str | None = None) -> list[BackupRow]:
    """Return every chain/run pair under ``BACKUP_PATH`` across all host_ids.

    Restricted to ``vm_uuid`` when supplied. Rejects any path that does not
    resolve safely under ``BACKUP_PATH`` so a stray symlink under an operator
    host directory cannot redirect enumeration outside the backup tree.
    """
    backup_path = config.path_value("BACKUP_PATH")
    if not config.get("BACKUP_PATH").strip() or not backup_path.is_dir():
        return []
    rows: list[BackupRow] = []
    for host_dir in _iter_host_dirs(backup_path):
        if not subpath_is_safe(backup_path, host_dir):
            continue
        for vm_dir in _iter_vm_dirs_for_host(host_dir, vm_uuid):
            if not subpath_is_safe(backup_path, vm_dir):
                continue
            for month, chain_id, chain_dir in _iter_chain_dirs(vm_dir):
                if not subpath_is_safe(backup_path, chain_dir):
                    continue
                rows.extend(_rows_for_chain(host_dir.name, vm_dir.name, month, chain_id, chain_dir))
    # Two-stage stable sort: descend by timestamp first, then stably sort by
    # (host_id, vm_uuid). Python's stable sort preserves the per-VM newest-
    # first ordering established by the first pass, while the second pass
    # keeps each VM's restore points grouped together in the listing. The
    # fish completion menu reads this output, so the operator sees the most
    # recent restore point at the top of the menu without an extra sort -r.
    rows.sort(key=lambda row: row.timestamp, reverse=True)
    rows.sort(key=lambda row: (row.host_id, row.vm_uuid))
    return rows


_HEADERS = ("VM_UUID", "TIMESTAMP", "VM_NAME", "HOST_ID", "KIND", "MONTH", "CHAIN_ID")


def format_rows(rows: list[BackupRow]) -> str:
    """Render rows as a fixed-width, space-aligned table.

    UUID and TIMESTAMP land in the first two columns so a copy-paste of "the
    first two words of any line" produces a valid ``restore`` invocation. The
    output is also tab-grep-friendly because every column is padded to the
    widest cell, so column N starts at the same offset on every row.
    """
    cells: list[tuple[str, ...]] = [_HEADERS]
    for row in rows:
        cells.append(
            (row.vm_uuid, row.timestamp, row.vm_name or "-", row.host_id, row.kind or "-", row.month, row.chain_id)
        )
    widths = [max(len(cell[i]) for cell in cells) for i in range(len(_HEADERS))]
    lines: list[str] = []
    for cell in cells:
        parts = [cell[i].ljust(widths[i]) for i in range(len(_HEADERS))]
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines)


def list_restore_points(config: Config) -> int:
    if not runtime_backup_path_ok(config):
        return 1
    rows = enumerate_backups(config)
    if not rows:
        event("info", "no backups found")
        return 0
    print(format_rows(rows))
    return 0
