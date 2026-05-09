from __future__ import annotations

import os
import shlex
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .logging_json import event

CONFIG_KEYS = {
    "LIBVIRT_URI",
    "BACKUP_PATH",
    "HOST_ID",
    "VM_BLACKLIST",
    "BACKUP_COMPRESS",
    "SYSTEMD_ON_CALENDAR",
    "BACKUP_REQUIRE_NFS_MOUNT",
    "BACKUP_RETENTION_MONTHS",
    "SPACE_MARGIN_PERCENT",
    "INACTIVE_COPY_EVERY_RUN",
    "BACKUP_ESTIMATE_GB_PER_VM",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    "REQUIRE_ROOT",
}


DEFAULTS = {
    "LIBVIRT_URI": "qemu:///system",
    "BACKUP_PATH": "",
    "HOST_ID": "",
    "VM_BLACKLIST": "",
    "BACKUP_COMPRESS": "true",
    "SYSTEMD_ON_CALENDAR": "*-*-* 02:30:00",
    "BACKUP_REQUIRE_NFS_MOUNT": "true",
    "BACKUP_RETENTION_MONTHS": "12",
    "SPACE_MARGIN_PERCENT": "20",
    "INACTIVE_COPY_EVERY_RUN": "false",
    "BACKUP_ESTIMATE_GB_PER_VM": "1",
    "BACKUP_INCREMENTAL_MULTIPLIER": "1.2",
    "REQUIRE_ROOT": "true",
}


COMMENTED_ENV_KEYS = {
    "LIBVIRT_URI",
    "HOST_ID",
    "VM_BLACKLIST",
    "BACKUP_COMPRESS",
    "SYSTEMD_ON_CALENDAR",
    "BACKUP_REQUIRE_NFS_MOUNT",
    "BACKUP_RETENTION_MONTHS",
    "SPACE_MARGIN_PERCENT",
    "INACTIVE_COPY_EVERY_RUN",
    "BACKUP_ESTIMATE_GB_PER_VM",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    "REQUIRE_ROOT",
}


ENV_TEMPLATE: tuple[str | None, ...] = (
    "# libvirt-backup-system environment file",
    "#",
    "# Installed path:",
    "#   /etc/libvirt-backup-system/libvirt-backup.env",
    "#",
    "# Values in the real process environment override values in this file.",
    "# Booleans are enabled by: 1, true, yes, on. Anything else is false.",
    None,
    "# Libvirt connection used by virsh for VM discovery and state checks.",
    "LIBVIRT_URI",
    None,
    "# Backup root. Backups are written as:",
    "#   BACKUP_PATH/<host-id>/<vm-name>/<yyyy-mm>/<timestamp>/",
    "BACKUP_PATH",
    None,
    '# Backup host folder name. Empty means "use this machine\'s short hostname".',
    "# Keep this stable: renaming HOST_ID orphans existing retention history",
    "# and inactive-copy markers under the previous folder, and cleanup will",
    "# stop pruning them.",
    "HOST_ID",
    None,
    "# VM names to skip. Separate with spaces or commas.",
    "VM_BLACKLIST",
    None,
    "# Add --compress to virtnbdbackup commands.",
    "BACKUP_COMPRESS",
    None,
    "# systemd OnCalendar value used when the timer unit is installed.",
    "# Re-run install or edit/reload the timer if this changes after install.",
    "SYSTEMD_ON_CALENDAR",
    None,
    "# Require BACKUP_PATH to be a mounted filesystem, usually an NFS/QNAP mount.",
    "# Set false when backing up to an intentionally local directory.",
    "BACKUP_REQUIRE_NFS_MOUNT",
    None,
    "# Number of monthly backup directories to keep per VM.",
    "# Set to -1 to keep all months (cleanup never prunes).",
    "# 0 is rejected by preflight to avoid an unintentional 'delete everything'.",
    "BACKUP_RETENTION_MONTHS",
    None,
    "# Extra free-space margin added to preflight's backup size estimate.",
    "SPACE_MARGIN_PERCENT",
    None,
    "# Stopped VMs are copied once per month by default.",
    "# Set true to copy stopped VMs on every run.",
    "INACTIVE_COPY_EVERY_RUN",
    None,
    "# Per-VM backup size estimate used by preflight space checks, in GB.",
    "# Used only as a fallback when disk introspection (virsh / qemu-img) fails.",
    "BACKUP_ESTIMATE_GB_PER_VM",
    None,
    "# Multiplier applied to the sum of VM disk virtual sizes when estimating",
    "# required backup space (accounts for incremental overhead and metadata).",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    None,
    "# Require preflight and run commands to execute as root.",
    "REQUIRE_ROOT",
)


def root_prefix(value: str | None = None) -> Path:
    raw = value if value is not None else os.environ.get("LIBVIRT_BACKUP_ROOT_PREFIX", "/")
    return Path(raw).resolve()


def prefixed(path: str | Path, prefix: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        return prefix / path
    return prefix / str(path).lstrip("/")


def default_config_path(prefix: Path | None = None) -> Path:
    return prefixed("/etc/libvirt-backup-system/libvirt-backup.env", prefix or root_prefix())


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            try:
                value = shlex.split(value)[0]
            except ValueError:
                value = value[1:-1]
        values[key] = value
    return values


def bool_value(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def int_value(values: dict[str, str], key: str) -> int:
    return int(values[key])


def float_value(values: dict[str, str], key: str) -> float:
    return float(values[key])


def split_words(value: str) -> list[str]:
    return [item.strip() for item in value.replace(",", " ").split() if item.strip()]


@dataclass(frozen=True)
class Config:
    values: dict[str, str]
    path: Path
    prefix: Path

    @classmethod
    def load(cls, config_path: str | None = None, prefix: str | None = None) -> Config:
        root = root_prefix(prefix)
        raw_path = config_path or os.environ.get("LIBVIRT_BACKUP_CONFIG") or str(default_config_path(root))
        path = Path(raw_path)
        values = dict(DEFAULTS)
        values.update(parse_env_file(path))
        for key in CONFIG_KEYS:
            if key in os.environ:
                env_value = os.environ[key]
                if values.get(key) != env_value:
                    event("info", "env override", key=key, source="environ")
                values[key] = env_value
        if not values.get("HOST_ID"):
            values["HOST_ID"] = socket.gethostname().split(".")[0]
        return cls(values=values, path=path, prefix=root)

    def get(self, key: str) -> str:
        return self.values[key]

    def path_value(self, key: str) -> Path:
        return Path(self.values[key])

    def enabled(self, key: str) -> bool:
        return bool_value(self.values[key])

    @property
    def blacklist(self) -> set[str]:
        return set(split_words(self.values["VM_BLACKLIST"]))

    def render_env(self) -> str:
        lines: list[str] = []
        for item in ENV_TEMPLATE:
            if item is None:
                lines.append("")
            elif item in DEFAULTS:
                prefix = "# " if item in COMMENTED_ENV_KEYS else ""
                lines.append(f"{prefix}{item}={self.values.get(item, DEFAULTS[item])}")
            else:
                lines.append(item)
        return "\n".join(lines) + "\n"


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def iter_month_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and len(path.name) == 7 and path.name[4] == "-")
