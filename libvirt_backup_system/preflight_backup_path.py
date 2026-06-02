from __future__ import annotations

import os
from pathlib import Path

from .config import Config
from .paths import backup_root
from .storage import subpath_is_safe

WRITE_PROBE_NAME = ".libvirt-backup-system-write-test"


def backup_path_is_mount(backup_path: Path) -> tuple[bool, str | None]:
    try:
        return backup_path.is_mount(), None
    except OSError as exc:
        return False, str(exc)


def write_probe(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    created = False
    try:
        fd = os.open(path, flags, 0o600)
        created = True
        if os.write(fd, b"ok\n") != 3:
            raise OSError("write probe was incomplete")
    finally:
        if fd is not None:
            os.close(fd)
        if created:
            path.unlink(missing_ok=True)


def validate_backup_path_readonly(config: Config) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        return []
    backup_path = config.path_value("BACKUP_PATH")
    if not backup_path.is_absolute():
        return ["BACKUP_PATH must be an absolute path"]
    if not backup_path.exists():
        return ["BACKUP_PATH must exist"]
    if not backup_path.is_dir():
        return ["BACKUP_PATH must be a directory"]
    if config.enabled("BACKUP_REQUIRE_NFS_MOUNT"):
        mounted, error = backup_path_is_mount(backup_path)
        if error is not None:
            return [f"BACKUP_PATH mount probe failed: {error}"]
        if not mounted:
            return ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]
    if not subpath_is_safe(backup_path, backup_root(config)):
        return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
    return []


def validate_backup_path_writable(config: Config) -> list[str]:
    failures = validate_backup_path_readonly(config)
    if failures or not config.get("BACKUP_PATH").strip():
        return failures
    backup_path = config.path_value("BACKUP_PATH")
    mount_required = config.enabled("BACKUP_REQUIRE_NFS_MOUNT")
    mount_msg = "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"
    try:
        host_root = backup_root(config)
        if mount_required:
            mounted, error = backup_path_is_mount(backup_path)
            if error is not None:
                return [f"BACKUP_PATH mount probe failed: {error}"]
            if not mounted:
                return [mount_msg]
        host_root.mkdir(parents=True, exist_ok=True)
        if not subpath_is_safe(backup_path, host_root):
            return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
        if mount_required:
            mounted, error = backup_path_is_mount(backup_path)
            if error is not None:
                return [f"BACKUP_PATH mount probe failed: {error}"]
            if not mounted:
                return [mount_msg]
        write_probe(host_root / WRITE_PROBE_NAME)
    except OSError as exc:
        return [f"BACKUP_PATH must be writable: {exc}"]
    return []
