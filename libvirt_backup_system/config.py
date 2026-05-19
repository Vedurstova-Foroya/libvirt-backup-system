from __future__ import annotations

import os
import shlex
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
    "SPACE_MARGIN_PERCENT",
    "BACKUP_ESTIMATE_GB_PER_VM",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    "REQUIRE_ROOT",
    "COMMAND_TIMEOUT_SECONDS",
    "BACKUP_RETENTION_MONTHS",
    "BACKUP_CLEANUP_ON_RUN",
}


DEFAULTS = {
    "LIBVIRT_URI": "qemu:///system",
    "BACKUP_PATH": "",
    "HOST_ID": "",
    "VM_BLACKLIST": "",
    "BACKUP_COMPRESS": "true",
    "SYSTEMD_ON_CALENDAR": "*-*-* 02:30:00",
    "BACKUP_REQUIRE_NFS_MOUNT": "true",
    "SPACE_MARGIN_PERCENT": "20",
    "BACKUP_ESTIMATE_GB_PER_VM": "1",
    "BACKUP_INCREMENTAL_MULTIPLIER": "1.2",
    "REQUIRE_ROOT": "true",
    "COMMAND_TIMEOUT_SECONDS": "86400",
    "BACKUP_RETENTION_MONTHS": "12",
    "BACKUP_CLEANUP_ON_RUN": "true",
}


COMMENTED_ENV_KEYS = {
    "LIBVIRT_URI",
    "HOST_ID",
    "VM_BLACKLIST",
    "BACKUP_COMPRESS",
    "SYSTEMD_ON_CALENDAR",
    "BACKUP_REQUIRE_NFS_MOUNT",
    "SPACE_MARGIN_PERCENT",
    "BACKUP_ESTIMATE_GB_PER_VM",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    "REQUIRE_ROOT",
    "COMMAND_TIMEOUT_SECONDS",
    "BACKUP_RETENTION_MONTHS",
    "BACKUP_CLEANUP_ON_RUN",
}


ENV_TEMPLATE: tuple[str | None, ...] = (
    "# libvirt-backup-system environment file",
    "#",
    "# Installed path:",
    "#   /etc/libvirt-backup-system/libvirt-backup.env",
    "#",
    "# Values in the real process environment override values in this file.",
    "# Booleans accept (case-insensitive): 1, true, yes, on as true;",
    "# 0, false, no, off as false. Any other value is rejected by preflight.",
    None,
    "# Libvirt connection used by virsh for VM discovery and state checks.",
    "LIBVIRT_URI",
    None,
    "# Backup root. Backups are written as:",
    "#   BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/",
    "# Only running VMs are backed up: the first run of each calendar month is",
    "# a full, later runs in the same month are incrementals into the same",
    "# chain-id directory. Offline VMs are logged and skipped.",
    "BACKUP_PATH",
    None,
    "# Require BACKUP_PATH to be a mounted filesystem, usually an NFS/QNAP mount.",
    "# Set false when backing up to an intentionally local directory.",
    "BACKUP_REQUIRE_NFS_MOUNT",
    None,
    '# Backup host folder name. Empty means "use /etc/machine-id".',
    "# Keep this stable: renaming HOST_ID writes new chains under a fresh folder",
    "# and leaves the old data untouched in the prior HOST_ID directory.",
    "HOST_ID",
    None,
    "# VM UUIDs to skip. Separate with spaces or commas.",
    "# Use ``virsh domuuid <vm-name>`` to look up a VM's UUID.",
    "VM_BLACKLIST",
    None,
    "# Add --compress to virtnbdbackup commands.",
    "BACKUP_COMPRESS",
    None,
    "# systemd OnCalendar value used when the timer unit is installed.",
    "# Run start after changing this so the timer is refreshed and reloaded.",
    "SYSTEMD_ON_CALENDAR",
    None,
    "# Extra free-space margin added to preflight's backup size estimate.",
    "SPACE_MARGIN_PERCENT",
    None,
    "# Per-VM backup size estimate used by preflight space checks, in GB.",
    "# Used only as a fallback when disk introspection (virsh / qemu-img) fails.",
    "BACKUP_ESTIMATE_GB_PER_VM",
    None,
    "# Multiplier applied to the sum of VM disk virtual sizes when estimating",
    "# required backup space. Accounts for compression overhead, metadata, and",
    "# per-VM safety margin on top of the raw disk virtual size.",
    "BACKUP_INCREMENTAL_MULTIPLIER",
    None,
    "# Require preflight and run commands to execute as root.",
    "REQUIRE_ROOT",
    None,
    "# Timeout for external commands, in seconds.",
    "COMMAND_TIMEOUT_SECONDS",
    None,
    "# Number of most-recent calendar months of backups to keep per VM. ``0``",
    "# disables pruning entirely. Default of 12 retains roughly one year.",
    "BACKUP_RETENTION_MONTHS",
    None,
    "# Run the monthly retention pass after every successful ``run``. Disable to",
    "# manage retention out-of-band; pruning failures never roll back backups.",
    "BACKUP_CLEANUP_ON_RUN",
)


def _read_machine_id(prefix: Path) -> str:
    path = prefixed("/etc/machine-id", prefix)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


# ``frozen=True`` here protects the three attribute bindings (``values``,
# ``path``, ``prefix``) from rebind after construction — it does NOT freeze the
# ``values`` dict's contents. ``installer.install`` deliberately mutates
# ``values`` in-place to apply ``INSTALL_TIME_ENV_KEYS`` from the process
# environment on a first install, and the unit tests rely on the same pattern.
# If you need a true immutable view, copy ``values`` at the boundary; do not
# remove ``frozen=True`` without auditing every install/test path.
@dataclass(frozen=True)
class Config:
    values: dict[str, str]
    path: Path
    prefix: Path

    @classmethod
    def load(
        cls,
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        root = root_prefix(prefix)
        raw_path = config_path or os.environ.get("LIBVIRT_BACKUP_CONFIG") or str(default_config_path(root))
        path = Path(raw_path)
        values = dict(DEFAULTS)
        values.update(parse_env_file(path))
        if apply_env_overrides:
            for key in CONFIG_KEYS:
                if key in os.environ:
                    env_value = os.environ[key]
                    if values.get(key) != env_value:
                        event("info", "env override", key=key, source="environ")
                    values[key] = env_value
        if not values.get("HOST_ID"):
            # Fall back to /etc/machine-id. If the file is missing or empty
            # leave HOST_ID="" so _validate_required_present surfaces a clean
            # "HOST_ID must not be empty".
            values["HOST_ID"] = _read_machine_id(root)
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


def is_month_dir_name(name: str) -> bool:
    # Calendar-month format YYYY-MM where month is 01-12. The fixed shape lets
    # verify, retention, and restore enumerate month dirs without touching any
    # operator junk that may have been dropped under a VM directory.
    if len(name) != 7 or name[4] != "-":
        return False
    year, month = name[:4], name[5:]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def iter_month_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and is_month_dir_name(path.name))
