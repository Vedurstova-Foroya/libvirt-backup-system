"""Per-run checkpoint records for chain-internal point-in-time restore."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from pathlib import Path

from .atomic_io import atomic_write
from .logging_json import event

RUNS_FILE = "runs.jsonl"
CHAIN_POISON_NAME = ".chain-poisoned"
CPT_SUFFIX = ".cpt"
CHECKPOINTS_SUBDIR = "checkpoints"


class CheckpointReadError(OSError):
    """Raised when checkpoint metadata exists on disk but cannot be read.

    Distinct from a missing file: a missing .cpt or checkpoints/ dir is a
    normal pre-first-backup state, but an existing file that we cannot read
    (permission flip, NFS error, fs corruption) hides whether virtnbdbackup
    actually wrote a new checkpoint. ``record_run`` translates this into a
    distinct error event and fails the run so the operator sees the problem
    at backup time instead of weeks later at restore time.
    """


def chain_poison_path(chain_dir: Path) -> Path:
    return chain_dir / CHAIN_POISON_NAME


def chain_is_poisoned(chain_dir: Path) -> bool:
    try:
        return stat.S_ISREG(chain_poison_path(chain_dir).lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        event("error", "chain poison sentinel check failed", chain_dir=str(chain_dir), error=str(exc))
        return True


def poison_chain(chain_dir: Path, vm_name: str, reason: str) -> bool:
    return atomic_write(
        chain_poison_path(chain_dir),
        reason.rstrip() + "\n",
        vm_name,
        "chain poison sentinel write failed",
    )


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


def list_checkpoints(chain_dir: Path, vm_name: str | None = None) -> set[str]:
    """Names of every checkpoint virtnbdbackup has written into ``chain_dir``.

    Reads the real virtnbdbackup state (``<vm>.cpt`` JSON list, then
    ``checkpoints/*.xml``). ``vm_name`` is required to locate the
    authoritative ``.cpt`` file; callers that have not yet been threaded
    through to pass a name skip straight to the XML directory fallback.

    Raises ``CheckpointReadError`` when a checkpoint source exists but is
    unreadable (permission flip, NFS hiccup, fs corruption). Missing
    sources are silently absent: the caller falls through to the next
    source. Returning an empty set on a real read error would let a
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
    return set()


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


def record_run(
    chain_dir: Path, stamp: str, before: set[str], vm_name: str | None = None, *, expect_new: bool = False
) -> bool:
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
        if expect_new:
            event("error", "expected new checkpoint but none appeared", chain_dir=str(chain_dir))
            return False
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
