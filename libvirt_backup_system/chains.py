from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .inactive_markers import atomic_write, marker_is_regular, stamp_is_safe
from .logging_json import event
from .run_records import chain_is_poisoned
from .storage import subpath_is_safe

# Single-file state lives directly under the month dir alongside the inactive
# marker. ``.`` prefix matches the existing hidden-state convention used by
# ``.inactive-copy-complete``.
CHAIN_STATE_NAME = ".chain-state.json"


@dataclass(frozen=True)
class ChainResolution:
    chain_dir: Path
    level: str
    is_new_chain: bool


def _backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _read_json_chain_state(path: Path) -> tuple[str | None, str | None]:
    if not marker_is_regular(path):
        return None, None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        event("error", "chain state read failed", path=str(path), error=str(exc))
        return None, None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        # A truncated or hand-edited file falls back to "no chain" so the next
        # run forces a new full rather than silently consuming garbage.
        event("error", "chain state JSON is malformed", path=str(path), error=str(exc))
        return None, None
    if not isinstance(data, dict):
        return None, None
    record: dict[str, object] = data
    raw_chain_id: object = record.get("chain_id")
    raw_fingerprint: object = record.get("fingerprint")
    chain_id = raw_chain_id if isinstance(raw_chain_id, str) and raw_chain_id else None
    fingerprint = raw_fingerprint if isinstance(raw_fingerprint, str) and raw_fingerprint else None
    return chain_id, fingerprint


def read_chain_state(month_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(chain_id, fingerprint)`` from the month dir, ``None`` when absent.

    Reads the single-file JSON state. The layout is written atomically
    (one rename, one durability event) so a half-written state — pointer
    present, fingerprint absent or vice versa — is impossible.
    """
    chain_id, fingerprint = _read_json_chain_state(month_dir / CHAIN_STATE_NAME)
    if chain_id is not None and fingerprint is not None:
        return chain_id, fingerprint
    return None, None


def write_chain_state(month_dir: Path, chain_id: str, fingerprint: str, vm_name: str) -> bool:
    """Atomically persist the current chain pointer + XML fingerprint.

    Writes both fields into a single JSON file via one ``atomic_write`` so a
    crash between two separate writes cannot leave a half-state that the
    next run interprets as "no chain" and answers with an unnecessary full.
    """
    payload = json.dumps({"chain_id": chain_id, "fingerprint": fingerprint}, sort_keys=True) + "\n"
    return atomic_write(
        month_dir / CHAIN_STATE_NAME,
        payload,
        vm_name,
        "chain state write failed",
    )


def _existing_chain_dir(config: Config, month_dir: Path, chain_id: str) -> Path | None:
    if not stamp_is_safe(chain_id):
        event(
            "error",
            "chain pointer is unsafe; starting new chain",
            month=str(month_dir),
            chain_id=chain_id,
        )
        return None
    candidate = month_dir / chain_id
    if not _backup_subpath_is_safe(config, candidate):
        event(
            "error",
            "chain dir path is unsafe; starting new chain",
            month=str(month_dir),
            chain_dir=str(candidate),
        )
        return None
    try:
        if candidate.is_dir():
            return candidate
    except OSError as exc:
        event("error", "chain dir check failed", chain_dir=str(candidate), error=str(exc))
        return None
    return None


def resolve_chain(
    config: Config,
    vm_name: str,
    month_dir: Path,
    stamp: str,
    pre_fingerprint: str,
) -> ChainResolution:
    """Pick the chain dir + virtnbdbackup level for this run.

    Returns the existing chain (``-l inc``) when the pointer + fingerprint
    match and the chain dir still exists; otherwise picks a new chain dir
    keyed by ``stamp`` and ``-l full``. Pointer/fingerprint are NOT written
    here — backup.py writes them only after the full succeeds, so a failed
    full does not strand a pointer to a non-existent chain.
    """
    pointer, stored_fingerprint = read_chain_state(month_dir)
    if pointer is None or stored_fingerprint is None:
        return ChainResolution(month_dir / stamp, "full", is_new_chain=True)
    if stored_fingerprint != pre_fingerprint:
        event(
            "info",
            "domain XML fingerprint changed; starting new chain",
            vm=vm_name,
            month=month_dir.name,
        )
        return ChainResolution(month_dir / stamp, "full", is_new_chain=True)
    existing = _existing_chain_dir(config, month_dir, pointer)
    if existing is None:
        event(
            "info",
            "previous chain dir missing; starting new chain",
            vm=vm_name,
            month=month_dir.name,
            previous_chain=pointer,
        )
        return ChainResolution(month_dir / stamp, "full", is_new_chain=True)
    if chain_is_poisoned(existing):
        event(
            "info",
            "current chain is poisoned; starting new chain",
            vm=vm_name,
            month=month_dir.name,
            previous_chain=pointer,
        )
        return ChainResolution(month_dir / stamp, "full", is_new_chain=True)
    return ChainResolution(existing, "inc", is_new_chain=False)
