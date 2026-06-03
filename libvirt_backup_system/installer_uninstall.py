from __future__ import annotations

import shutil
from pathlib import Path

from . import kopia_repo
from .config import Config, prefixed
from .fish_completion import remove_fish_completion
from .logging_json import event
from .shell import configure_default_timeout
from .systemd_units import (
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
    run_systemctl,
)

KOPIA_SYSTEMD_UNITS = (
    "libvirt-backup-system-maintenance.service",
    "libvirt-backup-system-maintenance.timer",
    "libvirt-backup-system-maintenance-full.service",
    "libvirt-backup-system-maintenance-full.timer",
    "libvirt-backup-system-verify.service",
    "libvirt-backup-system-verify.timer",
)
STATE_DIR = "/var/lib/libvirt-backup-system"


def remove_installed_files(root: Path) -> bool:
    ok = True
    for path in [
        prefixed("/usr/local/bin/libvirt-backup-system", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.service", root),
        prefixed("/etc/systemd/system/libvirt-backup-system-check.service", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.timer", root),
        *(prefixed(f"/etc/systemd/system/{name}", root) for name in KOPIA_SYSTEMD_UNITS),
    ]:
        try:
            path.unlink()
            event("info", "removed file", path=str(path))
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            event("error", "failed to remove file", path=str(path), error=str(exc))
            ok = False
    ok = remove_fish_completion(root) and ok
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    if opt_dir.exists():
        try:
            shutil.rmtree(opt_dir)
            event("info", "removed directory", path=str(opt_dir))
        except OSError as exc:
            event("error", "failed to remove directory", path=str(opt_dir), error=str(exc))
            ok = False
    return ok


def remove_stale_kopia_units(systemd_dir: Path) -> bool:
    ok = True
    for name in KOPIA_SYSTEMD_UNITS:
        path = systemd_dir / name
        try:
            path.unlink()
            event("info", "removed stale systemd unit", path=str(path))
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            event("error", "failed to remove stale systemd unit", path=str(path), error=str(exc))
            ok = False
    return ok


def uninstall_locked(
    root: Path,
    cfg: Config,
    *,
    purge_config: bool,
    purge_state: bool,
    purge_logs: bool,
) -> int:
    try:
        configure_default_timeout(cfg.get("COMMAND_TIMEOUT_SECONDS"))
    except ValueError as exc:
        event("warning", "invalid command timeout; uninstall continuing with default", error=str(exc))
    ok = run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
            ["systemctl", "disable", "--now", MAINTENANCE_TIMER_NAME],
            ["systemctl", "stop", MAINTENANCE_UNIT_NAME],
            ["systemctl", "disable", "--now", MAINTENANCE_FULL_TIMER_NAME],
            ["systemctl", "stop", MAINTENANCE_FULL_UNIT_NAME],
            ["systemctl", "disable", "--now", VERIFY_TIMER_NAME],
            ["systemctl", "stop", VERIFY_UNIT_NAME],
        ],
    )
    ok = remove_installed_files(root) and ok
    flags = {"config": purge_config, "state": purge_state, "logs": purge_logs}
    ok = (
        purge_paths(
            resolve_purge_paths(root, cfg, flags),
            preserve_paths=resolve_purge_preserve_paths(root, cfg, flags),
        )
        and ok
    )
    ok = run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    print_kopia_repo_retention_hint(cfg)
    return 0 if ok else 1


def print_kopia_repo_retention_hint(cfg: Config) -> None:
    backup_path = cfg.get("BACKUP_PATH").strip()
    if not backup_path:
        return
    try:
        repo_path = kopia_repo.local_repo_path(cfg)
    except ValueError:
        return
    event(
        "info",
        "kopia repo retained",
        path=str(repo_path),
        hint=f"rm -rf {repo_path} to delete backups",
    )


def resolve_purge_paths(root: Path, cfg: Config, flags: dict[str, bool]) -> list[Path]:
    paths: list[Path] = []
    if flags["config"]:
        paths.append(cfg.path)
    if flags["state"]:
        paths.append(prefixed(STATE_DIR, root))
    if flags["logs"]:
        paths.append(prefixed("/var/log/libvirt-backup-system", root))
    return paths


def resolve_purge_preserve_paths(root: Path, cfg: Config, flags: dict[str, bool]) -> list[Path]:
    candidates: list[Path] = []
    raw_password_file = cfg.get("KOPIA_PASSWORD_FILE").strip()
    if raw_password_file:
        candidates.append(prefixed(raw_password_file, root))
    raw_backup_path = cfg.get("BACKUP_PATH").strip()
    if raw_backup_path:
        candidates.append(prefixed(raw_backup_path, root))
    raw_repo_path = cfg.get("KOPIA_REPO_PATH").strip()
    if raw_repo_path:
        candidates.append(prefixed(raw_repo_path, root))
    elif raw_backup_path and cfg.get("HOST_ID").strip():
        candidates.append(prefixed(Path(raw_backup_path) / cfg.get("HOST_ID") / "kopia-repo", root))
    purge_roots = resolve_purge_paths(root, cfg, flags)
    return [
        path
        for index, path in enumerate(candidates)
        if path not in candidates[:index] and any(_is_preserved_by(path, purge_root) for purge_root in purge_roots)
    ]


def purge_paths(paths: list[Path], *, preserve_paths: list[Path] | None = None) -> bool:
    ok = True
    preserved = tuple(preserve_paths or [])
    for path in paths:
        if not path.exists():
            continue
        try:
            _purge_path(path, preserved)
            event("info", "purged", path=str(path))
        except OSError as exc:
            event("error", "purge failed", path=str(path), error=str(exc))
            ok = False
    return ok


def _purge_path(path: Path, preserved: tuple[Path, ...]) -> None:
    if _is_preserved(path, preserved):
        event("info", "preserved purge path", path=str(path))
        return
    if path.is_dir() and not path.is_symlink():
        if _has_preserved_descendant(path, preserved):
            _purge_directory_contents(path, preserved)
        else:
            shutil.rmtree(path)
        return
    path.unlink()


def _purge_directory_contents(path: Path, preserved: tuple[Path, ...]) -> None:
    for child in path.iterdir():
        if _is_preserved(child, preserved):
            event("info", "preserved purge path", path=str(child))
            continue
        if child.is_dir() and not child.is_symlink() and _has_preserved_descendant(child, preserved):
            _purge_directory_contents(child, preserved)
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


def _is_preserved(path: Path, preserved: tuple[Path, ...]) -> bool:
    return any(path == preserve for preserve in preserved)


def _has_preserved_descendant(path: Path, preserved: tuple[Path, ...]) -> bool:
    return any(_is_relative_to(preserve, path) for preserve in preserved)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_preserved_by(path: Path, purge_root: Path) -> bool:
    return path == purge_root or _is_relative_to(path, purge_root)
