from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

from .shell import CommandError, run

REMOTE_LIBVIRT_URI_PREFIXES = ("qemu+ssh://", "qemu+tcp://", "qemu+tls://")


def libvirt_uri_uses_remote_transport(uri: str) -> bool:
    return uri.startswith(REMOTE_LIBVIRT_URI_PREFIXES)


def _dump_domain_xml(uri: str, vm_name: str) -> str:
    # ``--inactive`` selects the persistent on-disk domain definition rather
    # than the runtime-augmented form libvirt exposes for a live domain. The
    # fingerprint must be stable across a stop/start cycle, so the persistent
    # XML is the right input.
    return run(["virsh", "-c", uri, "dumpxml", "--inactive", "--", vm_name]).stdout


# Element paths libvirt either re-renders on every dumpxml or that move when
# the libvirtd binary is upgraded — stripping them before fingerprinting keeps
# a routine libvirt point-release from invalidating every chain and forcing a
# global new-full. ``<mac>`` is included because libvirt regenerates the
# auto-allocated MAC on some interface definitions; keeping the surrounding
# ``<interface>`` element preserves topology detection.
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
    """Return a SHA256 over the persistent dumpxml for ``vm_name``, or ``None``.

    Stored next to the chain pointer so disk attach/detach, device topology
    changes, or any other domain XML edit forces a new chain on the next run
    rather than appending an incremental against state the previous chain no
    longer matches.

    Before hashing, libvirt-version-volatile fields (``<currentMemory>``,
    auto-generated ``<seclabel>``, ``<metadata>``, ``<memoryBacking>``, and
    regenerated ``mac`` attributes) are stripped so a libvirtd package
    upgrade does not falsely invalidate every chain and force a global
    new-full. The remaining structure still catches disks added/removed,
    device topology changes, and CPU/memory edits.
    """
    try:
        xml_text = _dump_domain_xml(uri, vm_name)
    except (CommandError, OSError):
        # OSError covers virsh missing/unspawnable; treat that the same as a
        # CommandError so callers fail closed instead of crashing backup
        # orchestration mid-run.
        return None
    canonical = _canonicalize_for_fingerprint(xml_text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def vm_disk_paths(uri: str, vm_name: str) -> list[str]:
    """Return disk source paths for ``vm_name`` parsed from libvirt domain XML.

    Used by the preflight space estimate to compute the sum of virtual disk
    sizes. ``virsh dumpxml`` is preferred over ``domblklist`` because the XML
    representation keeps attribute values intact (so paths containing
    whitespace survive). Only entries with ``device="disk"`` and a
    ``file``/``block`` source are returned.

    Raises ``ValueError`` if any ``device="disk"`` entry uses a source type we
    cannot stat locally (network, RBD, iSCSI, libvirt volumes, ...).
    """
    return _parse_disk_paths(_dump_domain_xml(uri, vm_name), vm_name)


def _parse_disk_paths(xml_text: str, vm_name: str) -> list[str]:
    try:
        # ``virsh dumpxml`` output comes from a local libvirtd we already shell
        # out to elsewhere — there is no untrusted XML source here.
        domain = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError as exc:
        raise ValueError(f"virsh dumpxml for {vm_name!r} produced unparseable XML") from exc
    paths: list[str] = []
    for disk in domain.findall("devices/disk"):
        if disk.get("device") != "disk":
            continue
        type_value = disk.get("type", "")
        attr = {"file": "file", "block": "dev"}.get(type_value)
        if attr is None:
            raise ValueError(
                f"unsupported disk type {type_value!r} for {vm_name!r}; " "cannot determine local disk path"
            )
        source = disk.find("source")
        value = source.get(attr) if source is not None else None
        if not value:
            raise ValueError(f"missing disk source for {vm_name!r}")
        paths.append(value)
    return paths
