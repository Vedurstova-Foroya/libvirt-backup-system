from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

from .config import Config, bool_value, int_value, iter_month_dirs
from .logging_json import event
from .preflight import sh_quote
from .shell import CommandError, run
from .vms import VM, list_vms


def current_month(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%SZ")


def backup_vm(config: Config, vm: VM, month: str, stamp: str) -> bool:
    month_dir = config.path_value("LOCAL_ROOT") / vm.name / month
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
        run(cmd)
    except CommandError as exc:
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


def sync_vm(config: Config, vm_name: str) -> bool:
    if not bool_value(config.get("REMOTE_ENABLED")):
        return True
    local_vm_dir = config.path_value("LOCAL_ROOT") / vm_name
    remote_dir = f"{config.get('REMOTE_DIR').rstrip('/')}/{config.get('HOST_ID')}/{vm_name}/"
    try:
        run(config.ssh_base + [config.remote_target, f"mkdir -p {sh_quote(remote_dir)}"])
        cmd = [
            "rsync",
            "-a",
            "-e",
            " ".join(config.ssh_base),
            str(local_vm_dir) + "/",
            f"{config.remote_target}:{remote_dir}",
        ]
        run(cmd)
        event("info", "rsync completed", vm=vm_name, remote_dir=remote_dir)
        return True
    except CommandError as exc:
        event("error", "rsync failed", vm=vm_name, stderr=exc.result.stderr.strip())
        return False


def run_backups(config: Config) -> int:
    vms = list_vms(config)
    month = current_month()
    stamp = timestamp()
    ok = True
    for vm in vms:
        if not backup_vm(config, vm, month, stamp):
            ok = False
            continue
        if not sync_vm(config, vm.name):
            ok = False
    return 0 if ok else 1


def cleanup(config: Config) -> int:
    local_root = config.path_value("LOCAL_ROOT")
    keep_local = int_value(config.values, "LOCAL_RETENTION_MONTHS")
    removed = 0
    for vm_dir in sorted(path for path in local_root.glob("*") if path.is_dir()):
        month_dirs = list(iter_month_dirs(vm_dir))
        for month_dir in month_dirs[:-keep_local] if keep_local > 0 else month_dirs:
            shutil.rmtree(month_dir)
            removed += 1
            event("info", "removed local backup month", path=str(month_dir))

    if bool_value(config.get("REMOTE_ENABLED")) and config.get("REMOTE_HOST") and config.get("REMOTE_DIR"):
        remote_root = f"{config.get('REMOTE_DIR').rstrip('/')}/{config.get('HOST_ID')}"
        keep_remote = int_value(config.values, "REMOTE_RETENTION_MONTHS")
        script = (
            f"set -eu; root={sh_quote(remote_root)}; keep={keep_remote}; "
            "test -d \"$root\" || exit 0; "
            "for vm in \"$root\"/*; do "
            "test -d \"$vm\" || continue; "
            "ls -1 \"$vm\" | grep -E '^[0-9]{4}-[0-9]{2}$' | sort | "
            "head -n \"$(($(ls -1 \"$vm\" | grep -E '^[0-9]{4}-[0-9]{2}$' | wc -l)-keep))\" | "
            "while read m; do rm -rf \"$vm/$m\"; done; "
            "done"
        )
        run(config.ssh_base + [config.remote_target, script], check=False)
    event("info", "cleanup completed", removed_local_months=removed)
    return 0


def verify(config: Config, vm_name: str | None = None) -> int:
    roots = [config.path_value("LOCAL_ROOT") / vm_name] if vm_name else sorted(config.path_value("LOCAL_ROOT").glob("*"))
    ok = True
    for vm_root in roots:
        if not vm_root.is_dir():
            continue
        for month_dir in iter_month_dirs(vm_root):
            for backup_dir in sorted(path for path in month_dir.iterdir() if path.is_dir()):
                try:
                    run(["virtnbdrestore", "-i", str(backup_dir), "-o", "verify"])
                    event("info", "verify passed", backup=str(backup_dir))
                except CommandError as exc:
                    event("error", "verify failed", backup=str(backup_dir), stderr=exc.result.stderr.strip())
                    ok = False
    return 0 if ok else 1


def restore_to_dir(source: str, target: str) -> int:
    target_path = Path(target)
    target_path.mkdir(parents=True, exist_ok=True)
    run(["virtnbdrestore", "-i", source, "-o", "restore", "-D", str(target_path)])
    event("info", "restore completed", source=source, target=str(target_path))
    return 0
