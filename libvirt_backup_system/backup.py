from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .cleanup import backup_root, cleanup, runtime_backup_path_ok
from .config import Config, iter_month_dirs
from .disks import domain_xml_fingerprint, inactive_marker_is_fresh
from .inactive_markers import (
    marked_backup_dir,
    marker_is_regular,
    remove_fingerprint,
    remove_marker,
    write_marker,
)
from .logging_json import event
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .vms import VM, is_safe_vm_name, list_vms

__all__ = [
    "backup_root",
    "backup_subpath_is_safe",
    "backup_vm",
    "cleanup",
    "current_month",
    "run_backups",
    "timestamp",
    "verify",
]


def current_month(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
    # Microsecond precision avoids collisions on rapid back-to-back runs.
    # An existence check in backup_vm still guards against clock jumps.
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S_%fZ")


def backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _ensure_backup_subpath_safe(config: Config, path: Path, message: str) -> bool:
    if backup_subpath_is_safe(config, path):
        return True
    event("error", message, path=str(path), backup_path=config.get("BACKUP_PATH"))
    return False


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


def _confirm_inactive_marker_still_fresh(backup_dir: Path, vm: VM, month: str) -> bool:
    # Re-check the backup directory exists immediately before returning, so a
    # concurrent cleanup that pruned it between marked_backup_dir() and here
    # forces a recopy rather than a false "already fresh" claim.
    try:
        if not backup_dir.is_dir():
            event(
                "info",
                "inactive marker backup directory disappeared, recopying",
                vm=vm.name,
                path=str(backup_dir),
            )
            return False
    except OSError as exc:
        event(
            "error",
            "inactive marker backup directory recheck failed",
            vm=vm.name,
            path=str(backup_dir),
            error=str(exc),
        )
        return False
    event("info", "inactive VM already copied this month", vm=vm.name, month=month)
    return True


def _finalize_inactive_marker(  # noqa: PLR0913
    config: Config,
    inactive_marker: Path,
    month_dir: Path,
    vm: VM,
    pre_fingerprint: str,
    stamp: str,
    dest: Path,
) -> bool:
    if not runtime_backup_path_ok(config):
        return False
    if not _ensure_backup_subpath_safe(
        config,
        month_dir,
        "inactive marker skipped because destination became unsafe",
    ):
        return False
    post_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
    if post_fingerprint is None:
        event("error", "inactive fingerprint computation failed", vm=vm.name)
        return False
    if post_fingerprint != pre_fingerprint:
        # Domain XML changed during the backup, so the just-written copy does
        # not match the current config. Skip the marker so the next run redoes
        # the copy rather than treating a stale snapshot as fresh.
        event("warning", "domain XML changed during inactive backup; not marking fresh", vm=vm.name)
        event("info", "backup completed", vm=vm.name, destination=str(dest))
        return True
    if not write_marker(inactive_marker, stamp, post_fingerprint, vm.name):
        return False
    remove_fingerprint(inactive_marker, vm.name)
    event("info", "backup completed", vm=vm.name, destination=str(dest))
    return True


def _maybe_reuse_inactive_backup(config: Config, vm: VM, month: str, month_dir: Path, inactive_marker: Path) -> bool:
    if not vm.inactive or config.enabled("INACTIVE_COPY_EVERY_RUN") or not marker_is_regular(inactive_marker):
        return False
    backup_dir = marked_backup_dir(config, month_dir, inactive_marker, vm.name)
    if backup_dir and inactive_marker_is_fresh(config.get("LIBVIRT_URI"), vm.name, inactive_marker):
        return _confirm_inactive_marker_still_fresh(backup_dir, vm, month)
    if backup_dir:
        event("info", "inactive marker is stale, recopying", vm=vm.name, month=month)
    return False


def _attempt_partial_cleanup(config: Config, dest: Path, vm_name: str) -> None:
    if backup_subpath_is_safe(config, dest) and runtime_backup_path_ok(config):
        _remove_partial_destination(dest, vm_name)
    else:
        event(
            "error",
            "partial backup removal skipped because destination is unsafe",
            vm=vm_name,
            path=str(dest),
        )


def backup_vm(config: Config, vm: VM, month: str, stamp: str) -> bool:
    if not is_safe_vm_name(vm.name):
        raise ValueError(f"refusing unsafe VM name: {vm.name!r}")
    if not runtime_backup_path_ok(config):
        return False
    month_dir = backup_root(config) / vm.name / month
    dest = month_dir / stamp
    if not _ensure_backup_subpath_safe(config, month_dir, "backup skipped because destination is unsafe"):
        return False

    inactive_marker = month_dir / ".inactive-copy-complete"
    if _maybe_reuse_inactive_backup(config, vm, month, month_dir, inactive_marker):
        return True

    if dest.exists():
        # Refuse to overwrite or clean up an existing backup directory: a
        # rapid retry or backward clock jump must never delete prior data.
        event("error", "backup destination already exists", vm=vm.name, destination=str(dest))
        return False

    pre_fingerprint: str | None = None
    if vm.inactive:
        pre_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
        if pre_fingerprint is None:
            event("error", "inactive fingerprint computation failed", vm=vm.name)
            return False

    if not runtime_backup_path_ok(config):
        return False
    month_dir.mkdir(parents=True, exist_ok=True)
    if not _ensure_backup_subpath_safe(config, month_dir, "backup skipped because destination became unsafe"):
        return False

    if not vm.inactive:
        remove_marker(inactive_marker, vm.name)
        remove_fingerprint(inactive_marker, vm.name)

    level = "copy" if vm.inactive else "auto"
    cmd = ["virtnbdbackup", "-U", config.get("LIBVIRT_URI"), "-d", vm.name, "-l", level, "-o", str(dest)]
    if config.enabled("BACKUP_COMPRESS"):
        cmd.append("--compress")

    event("info", "backup started", vm=vm.name, state=vm.state, backup_level=level, destination=str(dest))
    try:
        run_streamed(cmd)
    except CommandError as exc:
        if dest.exists():
            _attempt_partial_cleanup(config, dest, vm.name)
        event(
            "error",
            "backup failed",
            vm=vm.name,
            returncode=exc.result.returncode,
            stderr=exc.result.stderr.strip(),
        )
        return False

    if vm.inactive and pre_fingerprint is not None:
        return _finalize_inactive_marker(config, inactive_marker, month_dir, vm, pre_fingerprint, stamp, dest)
    event("info", "backup completed", vm=vm.name, destination=str(dest))
    return True


def run_backups(config: Config) -> int:
    vms = list_vms(config)
    month = current_month()
    stamp = timestamp()
    ok = True
    for vm in vms:
        if not backup_vm(config, vm, month, stamp):
            ok = False
    return 0 if ok else 1


def _iter_verify_targets(root: Path, vm_name: str | None) -> tuple[list[Path], bool]:
    if vm_name is not None:
        if not is_safe_vm_name(vm_name):
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
