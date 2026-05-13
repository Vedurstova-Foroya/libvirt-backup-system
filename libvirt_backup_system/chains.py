from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .inactive_markers import atomic_write, marker_is_regular, stamp_is_safe
from .logging_json import event
from .storage import subpath_is_safe

# Pointer file name lives directly under the month dir alongside the inactive
# marker. ``.`` prefix matches the existing hidden-state convention used by
# ``.inactive-copy-complete`` and the legacy ``.inactive-copy-fingerprint``.
CHAIN_POINTER_NAME = ".current-chain"
CHAIN_FINGERPRINT_NAME = ".chain-fingerprint"


@dataclass(frozen=True)
class ChainResolution:
    chain_dir: Path
    level: str
    is_new_chain: bool


def _backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _read_text(path: Path) -> str | None:
    if not marker_is_regular(path):
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        event("error", "chain state read failed", path=str(path), error=str(exc))
        return None


def read_chain_state(month_dir: Path) -> tuple[str | None, str | None]:
    """Return ``(chain_id, fingerprint)`` from the month dir, ``None`` when absent.

    Both files are written atomically by ``write_chain_state``. A partial
    state (one file present, the other missing or empty) means the prior write
    crashed mid-way; callers treat it as "no chain" and start a new one.
    """
    pointer = _read_text(month_dir / CHAIN_POINTER_NAME)
    fingerprint = _read_text(month_dir / CHAIN_FINGERPRINT_NAME)
    if pointer is None or not pointer:
        pointer = None
    if fingerprint is None or not fingerprint:
        fingerprint = None
    return pointer, fingerprint


def write_chain_state(month_dir: Path, chain_id: str, fingerprint: str, vm_name: str) -> bool:
    """Atomically persist the current chain pointer + XML fingerprint."""
    pointer_ok = atomic_write(
        month_dir / CHAIN_POINTER_NAME,
        f"{chain_id}\n",
        vm_name,
        "chain pointer write failed",
    )
    if not pointer_ok:
        return False
    return atomic_write(
        month_dir / CHAIN_FINGERPRINT_NAME,
        f"{fingerprint}\n",
        vm_name,
        "chain fingerprint write failed",
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
    return ChainResolution(existing, "inc", is_new_chain=False)
