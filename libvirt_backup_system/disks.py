from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .shell import CommandError, run


def vm_disk_paths(uri: str, vm_name: str) -> list[str]:
    """Return disk source paths for ``vm_name`` parsed from libvirt domain XML.

    ``virsh dumpxml`` is preferred over ``domblklist`` because the XML
    representation keeps attribute values intact (so paths containing
    whitespace survive). Only entries with ``device="disk"`` and a
    ``file``/``block`` source are returned.

    Raises ``ValueError`` if any ``device="disk"`` entry uses a source type we
    cannot stat locally (network, RBD, iSCSI, libvirt volumes, ...). Returning
    a silently-truncated list would let the inactive-marker freshness check
    decide the VM is up-to-date and skip the monthly copy.
    """
    result = run(["virsh", "-c", uri, "dumpxml", "--inactive", "--", vm_name])
    try:
        # ``virsh dumpxml`` output comes from a local libvirtd we already shell
        # out to elsewhere — there is no untrusted XML source here.
        domain = ET.fromstring(result.stdout)  # noqa: S314
    except ET.ParseError as exc:
        # An empty list cannot be distinguished from "no disks", which makes
        # the inactive-marker freshness check fall through to "fresh" and skip
        # the monthly copy. Raise so callers treat introspection as failed.
        raise ValueError(f"virsh dumpxml for {vm_name!r} produced unparseable XML") from exc
    paths: list[str] = []
    for disk in domain.findall("devices/disk"):
        if disk.get("device") != "disk":
            continue
        type_value = disk.get("type", "")
        attr = {"file": "file", "block": "dev"}.get(type_value)
        if attr is None:
            raise ValueError(
                f"unsupported disk type {type_value!r} for {vm_name!r}; "
                "cannot verify freshness against non-file/block sources"
            )
        source = disk.find("source")
        if source is None:
            continue
        value = source.get(attr)
        if value:
            paths.append(value)
    return paths


def disks_modified_after(disks: list[str], mtime: float) -> bool:
    """Return ``True`` if any disk has changed since ``mtime``.

    Block devices report unreliable inode mtimes (the device node is rarely
    rewritten when its contents change), so any block device is treated as
    modified to force a re-copy rather than risk skipping a stale backup.
    Any ``OSError`` while stat-ing is treated the same way.
    """
    for disk in disks:
        path = Path(disk)
        try:
            if path.is_block_device():
                return True
            if path.stat().st_mtime > mtime:
                return True
        except OSError:
            return True
    return False


def inactive_marker_is_fresh(uri: str, vm_name: str, marker: Path) -> bool:
    try:
        marker_mtime = marker.stat().st_mtime
    except OSError:
        return False
    try:
        disks = vm_disk_paths(uri, vm_name)
    except (CommandError, OSError, ValueError):
        return False
    if not disks:
        return True
    return not disks_modified_after(disks, marker_mtime)
