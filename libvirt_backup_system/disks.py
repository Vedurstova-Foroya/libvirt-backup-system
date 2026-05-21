from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .shell import run

REMOTE_LIBVIRT_URI_PREFIXES = ("qemu+ssh://", "qemu+tcp://", "qemu+tls://")


@dataclass(frozen=True)
class DiskEntry:
    """One ``(target, source)`` pair extracted from libvirt domain XML."""

    target: str
    source: Path


def libvirt_uri_uses_remote_transport(uri: str) -> bool:
    return uri.startswith(REMOTE_LIBVIRT_URI_PREFIXES)


def _dump_domain_xml(uri: str, vm_name: str) -> str:
    # ``--inactive`` selects the persistent on-disk domain definition rather
    # than the runtime-augmented form libvirt exposes for a live domain. The
    # manifest needs the stable form so a restore produces a domain that
    # libvirt would have accepted at backup time.
    return run(["virsh", "-c", uri, "dumpxml", "--inactive", "--", vm_name]).stdout


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
    return [str(entry.source) for entry in _parse_disk_entries(_dump_domain_xml(uri, vm_name), vm_name)]


def vm_disk_paths_with_targets(uri: str, vm_name: str) -> list[DiskEntry]:
    """Return ``(target, source)`` pairs for ``vm_name``.

    Same parsing as :func:`vm_disk_paths`, but yielded with the libvirt
    ``target dev`` so callers (backup, restore manifest) can join disk
    streams back to their original device names.
    """
    return _parse_disk_entries(_dump_domain_xml(uri, vm_name), vm_name)


def _parse_disk_entries(xml_text: str, vm_name: str) -> list[DiskEntry]:
    try:
        domain = ET.fromstring(xml_text)  # noqa: S314
    except ET.ParseError as exc:
        raise ValueError(f"virsh dumpxml for {vm_name!r} produced unparseable XML") from exc
    entries: list[DiskEntry] = []
    for disk in domain.findall("devices/disk"):
        if disk.get("device") != "disk":
            continue
        type_value = disk.get("type", "")
        attr = {"file": "file", "block": "dev"}.get(type_value)
        if attr is None:
            raise ValueError(f"unsupported disk type {type_value!r} for {vm_name!r}; cannot determine local disk path")
        source = disk.find("source")
        value = source.get(attr) if source is not None else None
        if not value:
            raise ValueError(f"missing disk source for {vm_name!r}")
        target_elem = disk.find("target")
        target = target_elem.get("dev") if target_elem is not None else None
        if not target:
            raise ValueError(f"missing target dev for disk in {vm_name!r}")
        entries.append(DiskEntry(target=target, source=Path(value)))
    return entries
