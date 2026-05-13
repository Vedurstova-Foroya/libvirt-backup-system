from __future__ import annotations

from pathlib import Path

from .config import Config
from .logging_json import event


def backup_root(config: Config) -> Path:
    return config.path_value("BACKUP_PATH") / config.get("HOST_ID")


def write_name_marker(dest: Path, vm_name: str) -> None:
    # Empty marker so operators can find the UUID dir via ``find -name
    # '<vm>.name'``. Captured per-backup so later renames don't rewrite older
    # markers; soft failure since the backup itself is unaffected.
    marker = dest / f"{vm_name}.name"
    try:
        marker.touch(exist_ok=False)
    except OSError as exc:
        event("warning", "vm-name marker not written", vm=vm_name, marker=str(marker), error=str(exc))


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
