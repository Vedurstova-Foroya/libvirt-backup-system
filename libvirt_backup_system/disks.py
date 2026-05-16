from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path

from .inactive_markers import read_fingerprint
from .shell import CommandError, run

REMOTE_LIBVIRT_URI_PREFIXES = ("qemu+ssh://", "qemu+tcp://", "qemu+tls://")


def libvirt_uri_uses_remote_transport(uri: str) -> bool:
    return uri.startswith(REMOTE_LIBVIRT_URI_PREFIXES)


def _dump_domain_xml(uri: str, vm_name: str) -> str:
    return run(["virsh", "-c", uri, "dumpxml", "--inactive", "--", vm_name]).stdout


# Element paths libvirt either re-renders on every dumpxml or that move when
# the libvirtd binary is upgraded — stripping them before fingerprinting keeps
# a routine libvirt point-release from invalidating every monthly inactive
# marker and forcing a global recopy. ``<mac>`` is included because libvirt
# regenerates the auto-allocated MAC on some interface definitions; keeping
# the surrounding ``<interface>`` element preserves topology detection.
_VOLATILE_FINGERPRINT_ELEMENTS = (
    "currentMemory",
    "seclabel",
    "metadata",
    "memoryBacking",
    "mac",
)


def _canonicalize_for_fingerprint(xml_text: str) -> str:
    try:
        # ``virsh dumpxml`` output is produced by a local libvirtd we already
        # shell out to elsewhere; there is no untrusted XML source here. If
        # parsing fails we fall back to the raw text so a fingerprint is
        # still produced.
        domain = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError:
        return xml_text
    for element_name in _VOLATILE_FINGERPRINT_ELEMENTS:
        for parent in domain.iter():
            for child in list(parent):
                if child.tag == element_name:
                    parent.remove(child)
    for element in domain.iter():
        # Collapse whitespace-only text/tail to empty so libvirtd's varying
        # indentation between versions does not perturb the fingerprint.
        if element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    return ET.tostring(domain, encoding="unicode")


def domain_xml_fingerprint(uri: str, vm_name: str) -> str | None:
    """Return a SHA256 over the inactive dumpxml for ``vm_name``, or ``None``.

    Stored next to the inactive marker so disk attach/detach, device topology
    changes, or any other domain XML edit invalidates the monthly copy even
    when the underlying disk files have mtimes older than the marker.

    Before hashing, libvirt-version-volatile fields (``<currentMemory>``,
    auto-generated ``<seclabel>``, ``<metadata>``, ``<memoryBacking>``, and
    regenerated ``mac`` attributes) are stripped so a libvirtd package
    upgrade does not falsely invalidate every monthly marker and force a
    global recopy. The remaining structure still catches disks added/removed,
    device topology changes, and CPU/memory edits.
    """
    try:
        xml_text = _dump_domain_xml(uri, vm_name)
    except (CommandError, OSError):
        # OSError covers virsh missing/unspawnable; treat that the same as a
        # CommandError so callers fail closed (stale marker) instead of crashing
        # backup orchestration mid-run.
        return None
    canonical = _canonicalize_for_fingerprint(xml_text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    return _parse_disk_paths(_dump_domain_xml(uri, vm_name), vm_name)


def _parse_disk_paths(xml_text: str, vm_name: str) -> list[str]:
    try:
        # ``virsh dumpxml`` output comes from a local libvirtd we already shell
        # out to elsewhere — there is no untrusted XML source here.
        domain = ET.fromstring(xml_text)  # noqa: S314
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
        value = source.get(attr) if source is not None else None
        if not value:
            raise ValueError(f"missing disk source for {vm_name!r}; cannot verify inactive backup freshness")
        paths.append(value)
    return paths


def disks_modified_after(disks: list[str], mtime: float) -> bool:
    """Return ``True`` if any disk has changed since ``mtime``.

    Block devices report unreliable inode mtimes (the device node is rarely
    rewritten when its contents change), so any block device is treated as
    modified to force a re-copy rather than risk skipping a stale backup.
    Any ``OSError`` while stat-ing is treated the same way. The mtime
    comparison is inclusive: on coarse-resolution filesystems (NFS, ext4 with
    one-second granularity) a disk written at the same wall-clock second as
    the marker would compare equal and be falsely reused, so equality is
    treated as modified.
    """
    for disk in disks:
        path = Path(disk)
        try:
            if path.is_block_device():
                return True
            if path.stat().st_mtime >= mtime:
                return True
        except OSError:
            return True
    return False


def inactive_marker_is_fresh(uri: str, vm_name: str, marker: Path) -> bool:
    try:
        marker_mtime = marker.stat().st_mtime
    except OSError:
        return False
    # Remote dumpxml paths belong to the hypervisor host, so local stat cannot
    # prove the inactive copy is fresh. Recopy instead of silently skipping.
    if libvirt_uri_uses_remote_transport(uri):
        return False
    fingerprint = domain_xml_fingerprint(uri, vm_name)
    if fingerprint is None:
        return False
    # An XML fingerprint mismatch catches domain edits — added disks, swapped
    # device files, CPU/memory changes — that a disk-mtime check alone would
    # miss. It also forces diskless VMs to recopy whenever their config moves
    # rather than being permanently treated as up-to-date.
    if read_fingerprint(marker) != fingerprint:
        return False
    try:
        disks = vm_disk_paths(uri, vm_name)
    except (CommandError, OSError, ValueError):
        return False
    if not disks:
        return True
    return not disks_modified_after(disks, marker_mtime)
