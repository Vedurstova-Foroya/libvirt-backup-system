"""Per-run manifest stored alongside disk snapshots in the kopia meta blob.

Each backup run writes one ``manifest.json`` into a fresh staging directory
that is then snapshotted as ``--tags kind:meta,run-id:<uuid>``. The disk
snapshots produced by the same run carry ``kind:disk,disk:<target>,run-id``
tags so restore can join them back to this manifest.

The format is owned by us, not by kopia. Reading it from disk lets restore
reconstruct the domain XML and pick the right destination paths without
re-asking libvirt — useful when the VM no longer exists on the local host.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from .atomic_io import atomic_write

MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True)
class ManifestDisk:
    """One disk row stored in the per-run manifest."""

    target: str
    source_path: str
    virtual_size_bytes: int
    snapshot_filename: str  # logical filename inside the kopia disk snapshot


@dataclass(frozen=True)
class Manifest:
    vm_name: str
    vm_uuid: str
    host_id: str
    run_id: str
    timestamp: str
    libvirt_uri: str
    domain_xml: str
    disks: tuple[ManifestDisk, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2) + "\n"

    def write(self, directory: Path, vm_name: str | None = None) -> bool:
        directory.mkdir(parents=True, exist_ok=True)
        return atomic_write(
            directory / MANIFEST_FILENAME,
            self.to_json(),
            vm_name or self.vm_name,
            "manifest write failed",
        )


def utc_timestamp(now: dt.datetime | None = None) -> str:
    """Return ``YYYYMMDDTHHMMSS`` UTC used as the per-run identifier."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S")


def parse_manifest(text: str) -> Manifest:
    """Round-trip parser for the manifest blob.

    Rejects payloads that do not match the expected shape so a corrupted or
    tampered file fails at read time rather than silently producing a partial
    restore.
    """
    parsed: object = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("manifest is not a JSON object")
    data = cast("dict[str, object]", parsed)
    raw_disks = data.get("disks")
    if not isinstance(raw_disks, list):
        raise ValueError("manifest disks field is not a list")
    disks: list[ManifestDisk] = []
    for disk in cast("list[object]", raw_disks):
        if not isinstance(disk, dict):
            raise ValueError("manifest disk row is not an object")
        disk_map = cast("dict[str, object]", disk)
        disks.append(
            ManifestDisk(
                target=_require_str(disk_map, "target"),
                source_path=_require_str(disk_map, "source_path"),
                virtual_size_bytes=_require_int(disk_map, "virtual_size_bytes"),
                snapshot_filename=_require_str(disk_map, "snapshot_filename"),
            )
        )
    return Manifest(
        vm_name=_require_str(data, "vm_name"),
        vm_uuid=_require_str(data, "vm_uuid"),
        host_id=_require_str(data, "host_id"),
        run_id=_require_str(data, "run_id"),
        timestamp=_require_str(data, "timestamp"),
        libvirt_uri=_require_str(data, "libvirt_uri"),
        domain_xml=_require_str(data, "domain_xml"),
        disks=tuple(disks),
    )


def read_manifest(path: Path) -> Manifest:
    return parse_manifest(path.read_text(encoding="utf-8"))


def _require_str(record: dict[str, object], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise ValueError(f"manifest field {key!r} must be a string")
    return value


def _require_int(record: dict[str, object], key: str) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"manifest field {key!r} must be an integer")
    return value


def snapshot_filename_for_target(target: str) -> str:
    """Stable mapping from libvirt target dev to per-disk snapshot filename.

    Used both by backup (when authoring the manifest + ``--stdin-file``) and
    by restore (to find the right file inside the kopia disk snapshot).
    """
    return f"{target}.raw"
