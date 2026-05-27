"""Disk-file helpers for overwrite restore."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path

from .logging_json import event
from .manifest import Manifest


def _overwrite_dest_map(manifest: Manifest) -> dict[str, Path]:
    return {disk.target: Path(disk.source_path) for disk in manifest.disks}


def _overwrite_temp_dest_map(dest_map: dict[str, Path]) -> dict[str, Path]:
    return {target: dest.with_name(f".{dest.name}.{target}.restore.tmp") for target, dest in dest_map.items()}


def _overwrite_backup_dest_map(dest_map: dict[str, Path]) -> dict[str, Path]:
    return {target: dest.with_name(f".{dest.name}.{target}.restore.old") for target, dest in dest_map.items()}


def _cleanup_paths(paths: dict[str, Path]) -> None:
    for path in paths.values():
        with suppress(FileNotFoundError):
            path.unlink()


def _rollback_overwrite_disks(backup_map: dict[str, Path], dest_map: dict[str, Path]) -> None:
    for target, backup in backup_map.items():
        if not backup.exists():
            continue
        dest = dest_map[target]
        with suppress(OSError):
            backup.replace(dest)


def _replace_overwrite_disks_with_backups(
    temp_map: dict[str, Path], dest_map: dict[str, Path]
) -> dict[str, Path] | None:
    backup_map = _overwrite_backup_dest_map(dest_map)
    _cleanup_paths(backup_map)
    for target, temp in temp_map.items():
        dest = dest_map[target]
        backup = backup_map[target]
        try:
            if dest.exists():
                dest.replace(backup)
            temp.replace(dest)
        except OSError as exc:
            event("error", "restored disk replace failed", target=target, src=str(temp), dest=str(dest), error=str(exc))
            _rollback_overwrite_disks(backup_map, dest_map)
            return None
    return backup_map


def _replace_overwrite_disks(temp_map: dict[str, Path], dest_map: dict[str, Path]) -> bool:
    backup_map = _replace_overwrite_disks_with_backups(temp_map, dest_map)
    if backup_map is None:
        return False
    _cleanup_paths(backup_map)
    return True
