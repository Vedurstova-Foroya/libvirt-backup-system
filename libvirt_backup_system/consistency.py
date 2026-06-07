"""Canonical backup consistency labels."""

from __future__ import annotations

CRASH = "crash"
FILESYSTEM = "filesystem"
UNKNOWN = "unknown"

_VALID = {CRASH, FILESYSTEM, UNKNOWN}


def parse_consistency(value: object) -> str:
    """Return a known consistency label, defaulting legacy/bad data to unknown."""
    return value if isinstance(value, str) and value in _VALID else UNKNOWN
