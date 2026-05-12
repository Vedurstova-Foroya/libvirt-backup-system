from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from pathlib import Path

from .cleanup import backup_root, cleanup, runtime_backup_path_ok
from .config import Config
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
from .verify import verify
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
    pre_copy_time: float,
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
        # Domain XML changed mid-backup; fail the VM so the run is non-zero and
        # cleanup is skipped (retention would otherwise prune known-good months
        # after this known-stale copy). Data is left on disk for inspection;
        # the missing marker forces a recopy on the next run.
        event("error", "domain XML changed during inactive backup; backup not trusted", vm=vm.name)
        return False
    if not write_marker(inactive_marker, stamp, post_fingerprint, vm.name):
        return False
    # Backdate the marker to the pre-copy timestamp so any mid-copy or
    # post-copy disk write has a newer mtime than the marker and forces a
    # recopy. A marker stamped "now" (post-copy) would falsely register a
    # mid-copy modification as fresh.
    try:
        os.utime(inactive_marker, (pre_copy_time, pre_copy_time))
    except OSError as exc:
        # Backdating is load-bearing for freshness, not a nice-to-have: a
        # marker with the default post-copy mtime can falsely classify a
        # mid-copy disk modification as still fresh. Roll the marker back and
        # fail this VM so the next run redoes the copy instead of trusting it.
        event("error", "inactive marker backdate failed; rolling back marker", vm=vm.name, error=str(exc))
        remove_marker(inactive_marker, vm.name)
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
    pre_copy_time: float = 0.0
    if vm.inactive:
        pre_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
        if pre_fingerprint is None:
            event("error", "inactive fingerprint computation failed", vm=vm.name)
            return False
        # Captured before the copy starts so the marker can be backdated to a
        # moment that pre-dates any possible mid-copy disk modification.
        pre_copy_time = time.time()

    if not runtime_backup_path_ok(config):
        return False
    try:
        month_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # A single VM's month-directory creation failure must not abort the
        # whole run. Log and fail this VM only; the run-level loop will move
        # on to the next VM rather than skipping every remaining one because
        # of a transient permission/quota issue on one path.
        event(
            "error",
            "backup skipped because month directory creation failed",
            vm=vm.name,
            path=str(month_dir),
            error=str(exc),
        )
        return False
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

    # A virtnbdbackup zero exit without a produced destination, or with ``dest``
    # swapped for a symlink mid-copy, must not be treated as success: retention
    # would otherwise prune older known-good months on the back of a hollow
    # write, and cleanup with BACKUP_RETENTION_MONTHS=-1 returns before
    # scanning for unsafe symlinks at all.
    if not dest.is_dir():
        event("error", "backup reported success but destination is missing", vm=vm.name, destination=str(dest))
        return False
    if not _ensure_backup_subpath_safe(config, dest, "backup destination became unsafe after virtnbdbackup"):
        return False

    if vm.inactive and pre_fingerprint is not None:
        return _finalize_inactive_marker(
            config,
            inactive_marker,
            month_dir,
            vm,
            pre_fingerprint,
            stamp,
            dest,
            pre_copy_time,
        )
    # Re-check BACKUP_REQUIRE_NFS_MOUNT after the running-VM copy completes:
    # virtnbdbackup writing to a path that lost its NFS mount mid-run would have
    # silently landed on the underlying local directory. Reporting that as
    # success would let cleanup proceed on data that is not on the intended
    # backup volume. Inactive VMs are protected via _finalize_inactive_marker.
    if not runtime_backup_path_ok(config):
        event("error", "backup completed but backup path is no longer mounted", vm=vm.name, destination=str(dest))
        return False
    event("info", "backup completed", vm=vm.name, destination=str(dest))
    return True


def run_backups(config: Config, *, month: str | None = None) -> int:
    vms = list_vms(config)
    # ``month`` is supplied by the ``run`` CLI so the value captured here is the
    # same one passed to cleanup. Computing it locally only happens when called
    # directly (e.g. tests); the CLI always pins it once at run start.
    month = month or current_month()
    stamp = timestamp()
    ok = True
    for vm in vms:
        if not backup_vm(config, vm, month, stamp):
            ok = False
    return 0 if ok else 1
