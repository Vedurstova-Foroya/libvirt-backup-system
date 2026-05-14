"""Per-run checkpoint records for chain-internal point-in-time restore.

Each successful backup run appends one JSON line to ``runs.jsonl`` in its
chain directory: ``{"ts": "<YYYYMMDDTHHMMSS>", "checkpoint": "<name>"}``.
``restore`` reads this file to resolve ``--at`` to a specific
``virtnbdrestore --until <checkpoint>`` target inside the selected chain,
so a target between the chain start and the latest incremental restores to
exactly that intermediate state instead of replaying through to chain end.

The new checkpoint name is observed by diffing the chain dir's
``*.checkpoint`` files before vs. after each virtnbdbackup invocation:
whichever name appeared is the one virtnbdbackup just created. This
avoids hard-coding the ``virtnbdbackup.N`` naming convention and survives
a virtnbdbackup version change that renumbers checkpoints.

A chain dir without ``runs.jsonl`` (legacy backups, or backups taken on a
host that predates this feature) falls back to chain-end semantics:
``select_checkpoint`` returns ``None`` and restore omits ``--until``.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .logging_json import event

RUNS_FILE = "runs.jsonl"
CHECKPOINT_SUFFIX = ".checkpoint"
# Matches backup.timestamp(): second-precision UTC, no offset suffix because
# the chain dir name uses the same format and is implicitly UTC.
STAMP_FORMAT = "%Y%m%dT%H%M%S"


def list_checkpoints(chain_dir: Path) -> set[str]:
    """Names (without ``.checkpoint`` suffix) of every checkpoint file in chain_dir."""
    try:
        entries = list(chain_dir.iterdir())
    except OSError:
        return set()
    return {entry.stem for entry in entries if entry.is_file() and entry.suffix == CHECKPOINT_SUFFIX}


def record_run(chain_dir: Path, stamp: str, before: set[str]) -> None:
    """Append a ``{ts, checkpoint}`` record for the checkpoint added by this run.

    ``before`` is the checkpoint set captured immediately prior to running
    virtnbdbackup. A run that did not write a new checkpoint is anomalous
    (virtnbdbackup always creates one for full/inc), so we log at info and
    skip: ``select_checkpoint`` will fall back to chain-end for any ``--at``
    that needed the missing record.
    """
    new = sorted(list_checkpoints(chain_dir) - before)
    if not new:
        event("info", "run recorded no new checkpoint", chain_dir=str(chain_dir))
        return
    runs_path = chain_dir / RUNS_FILE
    try:
        with runs_path.open("a", encoding="utf-8") as handle:
            for name in new:
                line = json.dumps({"ts": stamp, "checkpoint": name}, sort_keys=True, separators=(",", ":"))
                handle.write(line + "\n")
            handle.flush()
    except OSError as exc:
        event("error", "run record write failed", chain_dir=str(chain_dir), error=str(exc))


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


def select_checkpoint(chain_dir: Path, at: dt.datetime) -> str | None:
    """Latest checkpoint whose recorded run-time is at-or-before ``at``.

    Returns ``None`` to mean "restore the chain end" (omit ``--until``)
    in three cases: the chain has no runs.jsonl (legacy), every record is
    later than ``at`` (caller should not have reached this chain, but the
    fallback is safe), or ``at`` is at-or-after the last recorded run
    (the chain-end is the right answer and ``--until <last>`` would be
    redundant).
    """
    records = _parse_records(chain_dir)
    if not records:
        return None
    if at >= records[-1][0]:
        return None
    for stamp, checkpoint in reversed(records):
        if stamp <= at:
            return checkpoint
    return None
