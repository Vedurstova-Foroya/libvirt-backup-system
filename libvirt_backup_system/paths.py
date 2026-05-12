from __future__ import annotations

from pathlib import Path

from .config import Config
from .logging_json import event


def backup_root(config: Config) -> Path:
    return config.path_value("BACKUP_PATH") / config.get("HOST_ID")


def runtime_backup_path_ok(config: Config) -> bool:
    # Preflight enforces BACKUP_REQUIRE_NFS_MOUNT once at start-of-run. Every
    # filesystem mutation re-checks because the mount can disappear at any time
    # between preflight, mkdir, the backup itself, and marker writes.
    if not config.enabled("BACKUP_REQUIRE_NFS_MOUNT"):
        return True
    backup_path = config.path_value("BACKUP_PATH")
    if backup_path.is_mount():
        return True
    event("error", "BACKUP_PATH is no longer a mount point", backup_path=str(backup_path))
    return False
