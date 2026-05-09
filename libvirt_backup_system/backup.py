from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .config import Config, int_value, iter_month_dirs
from .disks import inactive_marker_is_fresh
from .logging_json import event
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe, unsafe_symlink_descendants
from .vms import VM, list_vms


def current_month(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def backup_root(config: Config) -> Path:
    return config.path_value("BACKUP_PATH") / config.get("HOST_ID")


def backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _ensure_backup_subpath_safe(config: Config, path: Path, message: str) -> bool:
    if backup_subpath_is_safe(config, path):
        return True
    event("error", message, path=str(path), backup_path=config.get("BACKUP_PATH"))
    return False


def _is_safe_vm_name(name: str) -> bool:
    return bool(name) and name not in {".", ".."} and not name.startswith("-") and "/" not in name and "\\" not in name


def _remove_partial_destination(dest: Path, vm_name: str) -> None:
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        event(
            "error",
            "partial backup removal failed",
            vm=vm_name,
            destination=str(dest),
            error=str(exc),
        )
        return
    if dest.exists():
        event("error", "partial backup removal incomplete", vm=vm_name, destination=str(dest))
    else:
        event("info", "removed partial backup", vm=vm_name, destination=str(dest))


def backup_vm(config: Config, vm: VM, month: str, stamp: str) -> bool:
    if vm.name.startswith("-"):
        raise ValueError(f"refusing VM name that begins with a dash: {vm.name!r}")
    month_dir = backup_root(config) / vm.name / month
    dest = month_dir / stamp
    if not _ensure_backup_subpath_safe(config, month_dir, "backup skipped because destination is unsafe"):
        return False

    inactive_marker = month_dir / ".inactive-copy-complete"
    if not vm.running and inactive_marker.exists() and not config.enabled("INACTIVE_COPY_EVERY_RUN"):
        if inactive_marker_is_fresh(config.get("LIBVIRT_URI"), vm.name, inactive_marker):
            event("info", "inactive VM already copied this month", vm=vm.name, month=month)
            return True
        event("info", "inactive marker is stale, recopying", vm=vm.name, month=month)

    month_dir.mkdir(parents=True, exist_ok=True)
    if not _ensure_backup_subpath_safe(config, month_dir, "backup skipped because destination became unsafe"):
        return False

    if vm.running and inactive_marker.exists():
        inactive_marker.unlink(missing_ok=True)

    level = "auto" if vm.running else "copy"
    cmd = ["virtnbdbackup", "-U", config.get("LIBVIRT_URI"), "-d", vm.name, "-l", level, "-o", str(dest)]
    if config.enabled("BACKUP_COMPRESS"):
        cmd.append("--compress")

    event("info", "backup started", vm=vm.name, state=vm.state, backup_level=level, destination=str(dest))
    try:
        run_streamed(cmd)
    except CommandError as exc:
        if dest.exists():
            if backup_subpath_is_safe(config, dest):
                _remove_partial_destination(dest, vm.name)
            else:
                event(
                    "error",
                    "partial backup removal skipped because destination is unsafe",
                    vm=vm.name,
                    path=str(dest),
                )
        event(
            "error",
            "backup failed",
            vm=vm.name,
            returncode=exc.result.returncode,
            stderr=exc.result.stderr.strip(),
        )
        return False

    if not vm.running:
        inactive_marker.write_text(stamp + "\n", encoding="utf-8")
    event("info", "backup completed", vm=vm.name, destination=str(dest))
    return True


def _prune_vm_dir(vm_dir: Path, keep: int, backup_path: Path) -> tuple[int, int]:
    removed = 0
    skipped = 0
    month_dirs = list(iter_month_dirs(vm_dir))
    prunable = month_dirs if keep == 0 else month_dirs[:-keep]
    for month_dir in prunable:
        if not subpath_is_safe(backup_path, month_dir):
            event("error", "month cleanup skipped because backup path is unsafe", path=str(month_dir))
            skipped += 1
            continue
        try:
            shutil.rmtree(month_dir)
        except OSError as exc:
            event("error", "month cleanup failed", path=str(month_dir), error=str(exc))
            skipped += 1
            continue
        removed += 1
        event("info", "removed backup month", path=str(month_dir))
    return removed, skipped


def _prune_month_dirs(root: Path, keep: int, backup_path: Path) -> tuple[int, int]:
    removed = 0
    skipped = 0
    if keep < 0:
        return removed, skipped
    if not subpath_is_safe(backup_path, root):
        event("error", "cleanup skipped because backup path is unsafe", path=str(root))
        return removed, skipped + 1
    if not root.exists():
        return removed, skipped
    for path in unsafe_symlink_descendants(backup_path, root):
        # Fail closed across the whole tree: a single unsafe symlink could redirect rmtree
        # outside the backup root, so abort cleanup for every VM rather than partially prune.
        event("error", "cleanup skipped because backup tree contains unsafe symlink", path=str(path))
        return removed, skipped + 1
    for vm_dir in sorted(root.iterdir()):
        if not vm_dir.is_dir():
            continue
        if not subpath_is_safe(backup_path, vm_dir):
            event("error", "VM cleanup skipped because backup path is unsafe", path=str(vm_dir))
            skipped += 1
            continue
        vm_removed, vm_skipped = _prune_vm_dir(vm_dir, keep, backup_path)
        removed += vm_removed
        skipped += vm_skipped
    return removed, skipped


def run_backups(config: Config) -> int:
    vms = list_vms(config)
    month = current_month()
    stamp = timestamp()
    ok = True
    for vm in vms:
        if not backup_vm(config, vm, month, stamp):
            ok = False
    return 0 if ok else 1


def cleanup(config: Config) -> int:
    path = config.path_value("BACKUP_PATH")
    keep = int_value(config.values, "BACKUP_RETENTION_MONTHS")
    removed, skipped = _prune_month_dirs(backup_root(config), keep, path)
    event("info", "cleanup completed", removed_backup_months=removed, skipped=skipped)
    return 0 if skipped == 0 else 1


def _iter_verify_targets(root: Path, vm_name: str | None) -> tuple[list[Path], bool]:
    if vm_name is not None:
        if not _is_safe_vm_name(vm_name):
            event("error", "verify target name is invalid", vm=vm_name)
            return [], False
        return [root / vm_name], True
    return sorted(root.glob("*")), True


def _verify_backup_dir(backup_dir: Path) -> bool:
    try:
        run_streamed(["virtnbdrestore", "-i", str(backup_dir), "-o", "verify"])
    except CommandError as exc:
        event("error", "verify failed", backup=str(backup_dir), stderr=exc.result.stderr.strip())
        return False
    event("info", "verify passed", backup=str(backup_dir))
    return True


def verify(config: Config, vm_name: str | None = None) -> int:
    root = backup_root(config)
    backup_path = config.path_value("BACKUP_PATH")
    roots, name_ok = _iter_verify_targets(root, vm_name)
    ok = name_ok
    verified = 0
    for vm_root in roots:
        if not subpath_is_safe(backup_path, vm_root):
            event("error", "verify skipped because path is unsafe", path=str(vm_root))
            ok = False
            continue
        if not vm_root.is_dir():
            if vm_name:
                event("error", "verify target not found", vm=vm_name, path=str(vm_root))
                ok = False
            continue
        for month_dir in iter_month_dirs(vm_root):
            if not subpath_is_safe(backup_path, month_dir):
                event("error", "verify skipped because month path is unsafe", path=str(month_dir))
                ok = False
                continue
            for backup_dir in sorted(path for path in month_dir.iterdir() if path.is_dir()):
                if not subpath_is_safe(backup_path, backup_dir):
                    event("error", "verify skipped because backup path is unsafe", path=str(backup_dir))
                    ok = False
                    continue
                if not _verify_backup_dir(backup_dir):
                    ok = False
                verified += 1
    if verified == 0:
        event("error", "verify found no backups", vm=vm_name or None, root=str(root))
        ok = False
    return 0 if ok else 1


def restore_to_dir(source: str, target: str, *, force: bool = False) -> int:
    target_path = Path(target)
    if target_path.is_symlink():
        event("error", "restore target is a symlink", target=str(target_path))
        return 1
    if target_path.exists():
        if not target_path.is_dir():
            event("error", "restore target is not a directory", target=str(target_path))
            return 1
        if any(target_path.iterdir()) and not force:
            event("error", "restore target is not empty (pass --force to override)", target=str(target_path))
            return 1
    target_path.mkdir(parents=True, exist_ok=True)
    run_streamed(["virtnbdrestore", "-i", source, "-o", "restore", "-D", str(target_path)])
    event("info", "restore completed", source=source, target=str(target_path))
    return 0
