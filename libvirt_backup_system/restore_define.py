from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .config import Config
from .logging_json import event
from .shell import CommandError, run

RESTORED_CONFIG_FILE = "libvirt-backup-system-restored.xml"


def _set_child_text(root: ET.Element, tag: str, value: str) -> None:
    child = root.find(tag)
    if child is None:
        child = ET.Element(tag)
        insert_at = 0
        name = root.find("name")
        if tag == "uuid" and name is not None:
            insert_at = list(root).index(name) + 1
        root.insert(insert_at, child)
    child.text = value


def _write_restored_identity(xml_path: Path, vm_uuid: str, name: str | None) -> bool:
    try:
        tree = ET.parse(xml_path)  # noqa: S314
    except (ET.ParseError, OSError) as exc:
        event("error", "restore adjusted domain XML is unusable", path=str(xml_path), error=str(exc))
        return False
    root = tree.getroot()
    if root.tag != "domain":
        event("error", "restore adjusted XML is not a libvirt domain", path=str(xml_path), root=root.tag)
        return False
    if name is not None:
        _set_child_text(root, "name", name)
    _set_child_text(root, "uuid", vm_uuid)
    try:
        tree.write(xml_path, encoding="unicode")
    except OSError as exc:
        event("error", "restore could not update adjusted domain XML", path=str(xml_path), error=str(exc))
        return False
    return True


def define_restored_domain(config: Config, xml_path: Path, vm_uuid: str, name: str | None) -> bool:
    if not _write_restored_identity(xml_path, vm_uuid, name):
        return False
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "define", str(xml_path)])
    except CommandError as exc:
        event("error", "define restored domain failed", config=str(xml_path), stderr=exc.result.stderr.strip())
        return False
    except OSError as exc:
        event("error", "virsh define unavailable", config=str(xml_path), error=str(exc))
        return False
    return True
