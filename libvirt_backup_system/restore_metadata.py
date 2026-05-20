from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .storage import subpath_is_safe
from .vms import is_safe_vm_name


@dataclass(frozen=True)
class BackupDomainConfig:
    name: str | None
    disk_paths: tuple[Path, ...]

    @property
    def disk_output_dir(self) -> Path | None:
        parents = {path.parent for path in self.disk_paths if path.is_absolute()}
        return parents.pop() if len(parents) == 1 else None


def _config_files(chain_dir: Path) -> list[Path]:
    try:
        configs = sorted(chain_dir.glob("vmconfig*.xml"), key=lambda path: path.name, reverse=True)
    except OSError:
        return []
    return [path for path in configs if subpath_is_safe(chain_dir, path)]


def _parse_config(path: Path) -> BackupDomainConfig | None:
    try:
        domain = ET.parse(path).getroot()  # noqa: S314
    except (ET.ParseError, OSError):
        return None
    raw_name = domain.findtext("name")
    name = raw_name.strip() if raw_name is not None and is_safe_vm_name(raw_name.strip()) else None
    disk_paths: list[Path] = []
    for disk in domain.findall("devices/disk"):
        if disk.get("type") != "file" or disk.get("device") != "disk":
            continue
        source = disk.find("source")
        value = source.get("file") if source is not None else None
        if value:
            disk_paths.append(Path(value))
    return BackupDomainConfig(name=name, disk_paths=tuple(disk_paths))


def read_backup_domain_config(chain_dir: Path) -> BackupDomainConfig:
    for path in _config_files(chain_dir):
        parsed = _parse_config(path)
        if parsed is not None:
            return parsed
    return BackupDomainConfig(name=None, disk_paths=())
