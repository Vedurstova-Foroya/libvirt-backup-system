from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .config import Config, int_value, iter_month_dirs
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


def backup_vm(config: Config, vm: VM, month: str, stamp: str) -> bool:
    if vm.name.startswith("-"):
        raise ValueError(f"refusing VM name that begins with a dash: {vm.name!r}")
    month_dir = backup_root(config) / vm.name / month
    dest = month_dir / stamp
    month_dir.mkdir(parents=True, exist_ok=True)

    inactive_marker = month_dir / ".inactive-copy-complete"
    if not vm.running and inactive_marker.exists() and not config.enabled("INACTIVE_COPY_EVERY_RUN"):
        event("info", "inactive VM already copied this month", vm=vm.name, month=month)
        return True

    level = "auto" if vm.running else "copy"
    cmd = ["virtnbdbackup", "-d", vm.name, "-l", level, "-o", str(dest)]
    if config.enabled("BACKUP_COMPRESS"):
        cmd.append("--compress")

    event("info", "backup started", vm=vm.name, state=vm.state, backup_level=level, destination=str(dest))
    try:
        run_streamed(cmd)
    except CommandError as exc:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
            event("info", "removed partial backup", vm=vm.name, destination=str(dest))
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


def _prune_month_dirs(root: Path, keep: int, backup_path: Path) -> int:
    removed = 0
    if keep < 0:
        return removed
    if not subpath_is_safe(backup_path, root):
        event("error", "cleanup skipped because backup path is unsafe", path=str(root))
        return removed
    if not root.exists():
        return removed
    for path in unsafe_symlink_descendants(backup_path, root):
        event("error", "cleanup skipped because backup tree contains unsafe symlink", path=str(path))
        return removed
    for vm_dir in sorted(root.iterdir()):
        if not vm_dir.is_dir():
            continue
        if not subpath_is_safe(backup_path, vm_dir):
            event("error", "VM cleanup skipped because backup path is unsafe", path=str(vm_dir))
            continue
        month_dirs = list(iter_month_dirs(vm_dir))
        prunable = month_dirs if keep == 0 else month_dirs[:-keep]
        for month_dir in prunable:
            if not subpath_is_safe(backup_path, month_dir):
                event("error", "month cleanup skipped because backup path is unsafe", path=str(month_dir))
                continue
            shutil.rmtree(month_dir)
            removed += 1
            event("info", "removed backup month", path=str(month_dir))
    return removed


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
    removed = _prune_month_dirs(backup_root(config), keep, path)
    event(
        "info",
        "cleanup completed",
        removed_backup_months=removed,
    )
    return 0


def verify(config: Config, vm_name: str | None = None) -> int:
    root = backup_root(config)
    roots = [root / vm_name] if vm_name else sorted(root.glob("*"))
    ok = True
    for vm_root in roots:
        if not vm_root.is_dir():
            continue
        for month_dir in iter_month_dirs(vm_root):
            for backup_dir in sorted(path for path in month_dir.iterdir() if path.is_dir()):
                try:
                    run_streamed(["virtnbdrestore", "-i", str(backup_dir), "-o", "verify"])
                    event("info", "verify passed", backup=str(backup_dir))
                except CommandError as exc:
                    event("error", "verify failed", backup=str(backup_dir), stderr=exc.result.stderr.strip())
                    ok = False
    return 0 if ok else 1


def restore_to_dir(source: str, target: str) -> int:
    target_path = Path(target)
    target_path.mkdir(parents=True, exist_ok=True)
    run_streamed(["virtnbdrestore", "-i", source, "-o", "restore", "-D", str(target_path)])
    event("info", "restore completed", source=source, target=str(target_path))
    return 0
