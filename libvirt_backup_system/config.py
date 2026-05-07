from __future__ import annotations

import os
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


CONFIG_KEYS = {
    "LIBVIRT_URI",
    "LOCAL_ROOT",
    "HOST_ID",
    "VM_BLACKLIST",
    "BACKUP_COMPRESS",
    "SYSTEMD_ON_CALENDAR",
    "REMOTE_ENABLED",
    "REMOTE_HOST",
    "REMOTE_USER",
    "REMOTE_DIR",
    "SSH_KEY",
    "SSH_PORT",
    "SSH_OPTIONS",
    "LOCAL_RETENTION_MONTHS",
    "REMOTE_RETENTION_MONTHS",
    "SPACE_MARGIN_PERCENT",
    "MAX_PARALLEL_VMS",
    "INACTIVE_COPY_EVERY_RUN",
    "BACKUP_ESTIMATE_GB_PER_VM",
    "REQUIRE_ROOT",
}


DEFAULTS = {
    "LIBVIRT_URI": "qemu:///system",
    "LOCAL_ROOT": "/var/backups/libvirt",
    "HOST_ID": socket.gethostname().split(".")[0],
    "VM_BLACKLIST": "",
    "BACKUP_COMPRESS": "true",
    "SYSTEMD_ON_CALENDAR": "*-*-* 02:30:00",
    "REMOTE_ENABLED": "true",
    "REMOTE_HOST": "",
    "REMOTE_USER": "",
    "REMOTE_DIR": "",
    "SSH_KEY": "",
    "SSH_PORT": "22",
    "SSH_OPTIONS": "-o BatchMode=yes -o StrictHostKeyChecking=accept-new",
    "LOCAL_RETENTION_MONTHS": "2",
    "REMOTE_RETENTION_MONTHS": "12",
    "SPACE_MARGIN_PERCENT": "20",
    "MAX_PARALLEL_VMS": "1",
    "INACTIVE_COPY_EVERY_RUN": "false",
    "BACKUP_ESTIMATE_GB_PER_VM": "1",
    "REQUIRE_ROOT": "true",
}


def root_prefix(value: str | None = None) -> Path:
    raw = value or os.environ.get("LIBVIRT_BACKUP_ROOT_PREFIX", "/")
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
            value = shlex.split(value)[0] if value else ""
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
    def load(cls, config_path: str | None = None, prefix: str | None = None) -> "Config":
        root = root_prefix(prefix)
        path = Path(config_path) if config_path else Path(os.environ.get("LIBVIRT_BACKUP_CONFIG", default_config_path(root)))
        values = dict(DEFAULTS)
        values.update(parse_env_file(path))
        for key in CONFIG_KEYS:
            if key in os.environ:
                values[key] = os.environ[key]
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

    @property
    def ssh_base(self) -> list[str]:
        cmd = ["ssh", "-p", self.values["SSH_PORT"]]
        if self.values["SSH_KEY"]:
            cmd.extend(["-i", self.values["SSH_KEY"]])
        cmd.extend(shlex.split(self.values["SSH_OPTIONS"]))
        return cmd

    @property
    def remote_target(self) -> str:
        user = self.values["REMOTE_USER"]
        host = self.values["REMOTE_HOST"]
        return f"{user}@{host}" if user else host

    def render_env(self) -> str:
        lines = [
            "# libvirt-backup-system configuration",
            "# Edit these values for the production host before enabling the timer.",
        ]
        for key in DEFAULTS:
            lines.append(f"{key}={self.values.get(key, DEFAULTS[key])}")
        return "\n".join(lines) + "\n"


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def iter_month_dirs(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and len(path.name) == 7 and path.name[4] == "-")
