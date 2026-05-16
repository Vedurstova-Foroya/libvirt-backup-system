from __future__ import annotations

import shutil
from pathlib import Path

from .config import Config, prefixed
from .logging_json import event


def remove_installed_files(root: Path) -> bool:
    ok = True
    for path in [
        prefixed("/usr/local/bin/libvirt-backup-system", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.service", root),
        prefixed("/etc/systemd/system/libvirt-backup-system-check.service", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.timer", root),
    ]:
        try:
            path.unlink()
            event("info", "removed file", path=str(path))
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            event("error", "failed to remove file", path=str(path), error=str(exc))
            ok = False
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    if opt_dir.exists():
        try:
            shutil.rmtree(opt_dir)
            event("info", "removed directory", path=str(opt_dir))
        except OSError as exc:
            event("error", "failed to remove directory", path=str(opt_dir), error=str(exc))
            ok = False
    return ok


def resolve_purge_paths(root: Path, cfg: Config, flags: dict[str, bool]) -> list[Path]:
    paths: list[Path] = []
    if flags["config"]:
        paths.append(cfg.path)
    if flags["state"]:
        paths.append(prefixed("/var/lib/libvirt-backup-system", root))
    if flags["logs"]:
        paths.append(prefixed("/var/log/libvirt-backup-system", root))
    return paths


def purge_paths(paths: list[Path]) -> bool:
    ok = True
    for path in paths:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            event("info", "purged", path=str(path))
        except OSError as exc:
            event("error", "purge failed", path=str(path), error=str(exc))
            ok = False
    return ok
