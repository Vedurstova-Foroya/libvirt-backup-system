"""Per-run checkpoint records for chain-internal point-in-time restore.

Each successful backup run appends one JSON line to ``runs.jsonl`` in its
chain directory: ``{"ts": "<YYYYMMDDTHHMMSS>", "checkpoint": "<name>"}``.
``restore`` reads this file to resolve ``--at`` to a specific
``virtnbdrestore --until <checkpoint>`` target inside the selected chain,
so a target between the chain start and the latest incremental restores to
exactly that intermediate state instead of replaying through to chain end.

The new checkpoint name is observed by diffing the chain dir's checkpoint
state before vs. after each virtnbdbackup invocation: whichever name
appeared is the one virtnbdbackup just created. ``list_checkpoints``
inspects three sources, in order:

1. ``<chain>/<vm-name>.cpt`` — the JSON list virtnbdbackup itself maintains
   (libvirtnbdbackup/virt/checkpoint.py ``save``). Authoritative when present.
2. ``<chain>/checkpoints/*.xml`` — the per-checkpoint libvirt XML the same
   binary writes alongside ``.cpt``. Used when ``.cpt`` is unreadable but
   the XML dir exists (matches the layout the real-KVM e2e asserts on).
3. ``<chain>/*.checkpoint`` — legacy/synthetic layout used by fixtures
   that pre-date the real-binary integration; kept for back-compat so older
   chain dirs and unit fixtures still work.

This avoids hard-coding the ``virtnbdbackup.N`` naming convention and
survives a virtnbdbackup version change that renumbers checkpoints.

A chain dir without ``runs.jsonl`` (legacy backups, or backups taken on a
host that predates this feature) falls back to chain-end semantics:
``select_checkpoint`` reports ``LEGACY`` and restore omits ``--until``.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .logging_json import event

RUNS_FILE = "runs.jsonl"
CHECKPOINT_SUFFIX = ".checkpoint"
CPT_SUFFIX = ".cpt"
CHECKPOINTS_SUBDIR = "checkpoints"
# Matches backup.timestamp(): second-precision UTC, no offset suffix because
# the chain dir name uses the same format and is implicitly UTC.
STAMP_FORMAT = "%Y%m%dT%H%M%S"


class CheckpointReadError(OSError):
    """Raised when checkpoint metadata exists on disk but cannot be read.

    Distinct from a missing file: a missing .cpt or checkpoints/ dir is a
    normal pre-first-backup state, but an existing file that we cannot read
    (permission flip, NFS error, fs corruption) hides whether virtnbdbackup
    actually wrote a new checkpoint. ``record_run`` translates this into a
    distinct error event and fails the run so the operator sees the problem
    at backup time instead of weeks later at restore time.
    """


def _read_cpt_file(cpt_path: Path) -> set[str] | None:
    """Parse the virtnbdbackup ``<domain>.cpt`` JSON-list file, or None.

    Returns ``None`` for a missing file (pre-first-backup; the caller falls
    back to other sources) or a wrong-shape JSON payload (treated as legacy
    layout). Raises ``CheckpointReadError`` for any other I/O failure so the
    caller can fail the run rather than silently treating the chain as having
    no checkpoint at all.
    """
    try:
        raw = cpt_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointReadError(f"reading {cpt_path}: {exc}") from exc
    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    items: list[object] = data
    return {item for item in items if isinstance(item, str) and item}


def _read_checkpoint_xml_dir(xml_dir: Path) -> set[str] | None:
    try:
        entries = list(xml_dir.iterdir())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CheckpointReadError(f"listing {xml_dir}: {exc}") from exc
    names = {entry.stem for entry in entries if entry.is_file() and entry.suffix == ".xml"}
    return names if names else None


def _read_legacy_checkpoint_files(chain_dir: Path) -> set[str]:
    try:
        entries = list(chain_dir.iterdir())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise CheckpointReadError(f"listing {chain_dir}: {exc}") from exc
    return {entry.stem for entry in entries if entry.is_file() and entry.suffix == CHECKPOINT_SUFFIX}


def list_checkpoints(chain_dir: Path, vm_name: str | None = None) -> set[str]:
    """Names of every checkpoint virtnbdbackup has written into ``chain_dir``.

    Reads the real virtnbdbackup state first (``<vm>.cpt`` JSON list, then
    ``checkpoints/*.xml``) and falls back to legacy top-level
    ``*.checkpoint`` files used by older fixtures. ``vm_name`` is required
    to locate the authoritative ``.cpt`` file; callers that have not yet
    been threaded through to pass a name skip straight to the fallbacks.

    Raises ``CheckpointReadError`` when a checkpoint source exists but is
    unreadable (permission flip, NFS hiccup, fs corruption). Missing
    sources are silently absent: the caller falls through to the next
    layout. Returning an empty set on a real read error would let a
    backup record success while restore --at later diverged from operator
    intent, so the caller must catch CheckpointReadError and fail the run.
    """
    if vm_name:
        from_cpt = _read_cpt_file(chain_dir / f"{vm_name}{CPT_SUFFIX}")
        if from_cpt is not None:
            return from_cpt
    xml_dir = chain_dir / CHECKPOINTS_SUBDIR
    if xml_dir.is_dir():
        from_xml = _read_checkpoint_xml_dir(xml_dir)
        if from_xml is not None:
            return from_xml
    return _read_legacy_checkpoint_files(chain_dir)


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        # Some filesystems (e.g. certain NFS configurations) refuse to open
        # directories for fsync. The append is still safer than no fsync — the
        # directory entry just falls back to "best effort durable".
        return
    try:
        with suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def record_run(chain_dir: Path, stamp: str, before: set[str], vm_name: str | None = None) -> bool:
    """Append a ``{ts, checkpoint}`` record for the checkpoint added by this run.

    ``before`` is the checkpoint set captured immediately prior to running
    virtnbdbackup. Returns ``True`` when the record was durably written, or
    when the caller can safely treat the absence as benign (no new checkpoint
    appeared, which means restore --at falls through to chain-end semantics
    naturally). Returns ``False`` when the record cannot be persisted: a
    chain-end fallback there would silently hand restore --at a newer state
    than the operator asked for, so callers must fail the run instead.
    """
    try:
        current = list_checkpoints(chain_dir, vm_name)
    except CheckpointReadError as exc:
        # Cannot tell whether virtnbdbackup added a new checkpoint, so we
        # also cannot durably record one. Failing the run surfaces the
        # underlying I/O problem now instead of letting restore --at later
        # report "no record found" against state we never observed.
        event("error", "checkpoint metadata read failed", chain_dir=str(chain_dir), error=str(exc))
        return False
    new = sorted(current - before)
    if not new:
        # virtnbdbackup did not add a checkpoint. That is anomalous (full/inc
        # both create one) but ``select_checkpoint`` does the right thing for
        # any ``--at`` in this case — there is no run record to disagree with
        # the chain contents — so the run itself can continue.
        event("info", "run recorded no new checkpoint", chain_dir=str(chain_dir))
        return True
    runs_path = chain_dir / RUNS_FILE
    try:
        with runs_path.open("a", encoding="utf-8") as handle:
            for name in new:
                line = json.dumps({"ts": stamp, "checkpoint": name}, sort_keys=True, separators=(",", ":"))
                handle.write(line + "\n")
            # fsync the data first so a crash between append and rename window
            # cannot leave a half-written line that ``_parse_records`` would
            # later skip as corrupt.
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        event("error", "run record write failed", chain_dir=str(chain_dir), error=str(exc))
        return False
    # fsync the parent directory so the new bytes survive a crash even if the
    # file existed before this append: ext4/xfs need a directory fsync to
    # commit the size update for an appended file.
    _fsync_directory(chain_dir)
    return True


def _parse_records(chain_dir: Path) -> list[tuple[dt.datetime, str]]:
    try:
        raw = (chain_dir / RUNS_FILE).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[tuple[dt.datetime, str]] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            ts = dt.datetime.strptime(data["ts"], STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
            checkpoint = data["checkpoint"]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            # Skip corrupt lines (truncated by power loss, hand-edited, ...);
            # the surviving records still let restore reach the right run.
            continue
        if isinstance(checkpoint, str) and checkpoint:
            records.append((ts, checkpoint))
    records.sort(key=lambda item: item[0])
    return records


class SelectStatus(enum.Enum):
    """Outcome of a ``select_checkpoint`` call.

    ``FOUND``: a matching checkpoint was found; use ``virtnbdrestore --until``.
    ``CHAIN_END``: ``at`` is at-or-after the last recorded run; omit ``--until``.
    ``LEGACY``: chain has no ``runs.jsonl``; omit ``--until`` (back-compat).
    ``MISSING``: ``runs.jsonl`` exists but has no record at-or-before ``at`` —
        either every record is in the future, or the older records are
        corrupt/truncated. Callers should refuse the restore rather than
        silently fall back to chain end, because chain end may be newer
        than the operator asked for.
    """

    FOUND = "found"
    CHAIN_END = "chain_end"
    LEGACY = "legacy"
    MISSING = "missing"


@dataclass(frozen=True)
class Selection:
    checkpoint: str | None
    status: SelectStatus


def select_checkpoint(chain_dir: Path, at: dt.datetime) -> Selection:
    """Latest checkpoint whose recorded run-time is at-or-before ``at``.

    The returned ``Selection`` distinguishes the three "omit ``--until``"
    cases from a genuine "no matching record" so the caller can refuse the
    restore for the latter rather than silently restoring chain end.
    """
    if not (chain_dir / RUNS_FILE).is_file():
        return Selection(None, SelectStatus.LEGACY)
    records = _parse_records(chain_dir)
    if not records:
        # File present but unreadable / every line corrupt: cannot prove
        # what was captured here, so refuse rather than guess.
        return Selection(None, SelectStatus.MISSING)
    if at >= records[-1][0]:
        return Selection(None, SelectStatus.CHAIN_END)
    for stamp, checkpoint in reversed(records):
        if stamp <= at:
            return Selection(checkpoint, SelectStatus.FOUND)
    return Selection(None, SelectStatus.MISSING)
