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
    """
    result = run(["virsh", "-c", uri, "dumpxml", "--inactive", "--", vm_name])
    try:
        # ``virsh dumpxml`` output comes from a local libvirtd we already shell
        # out to elsewhere — there is no untrusted XML source here.
        domain = ET.fromstring(result.stdout)  # noqa: S314
    except ET.ParseError:
        return []
    paths: list[str] = []
    for disk in domain.findall("devices/disk"):
        if disk.get("device") != "disk":
            continue
        source = disk.find("source")
        if source is None:
            continue
        attr = {"file": "file", "block": "dev"}.get(disk.get("type", ""))
        if attr is None:
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
    except (CommandError, OSError):
        return False
    if not disks:
        return True
    return not disks_modified_after(disks, marker_mtime)
