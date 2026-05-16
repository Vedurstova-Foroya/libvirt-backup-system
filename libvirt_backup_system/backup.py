from __future__ import annotations

import datetime as dt
import os
import shutil
import time
from pathlib import Path

from .chains import ChainResolution, resolve_chain, write_chain_state
from .config import Config
from .disks import domain_xml_fingerprint, inactive_marker_is_fresh
from .inactive_markers import marked_backup_dir, marker_is_regular, remove_fingerprint, remove_marker, write_marker
from .logging_json import event
from .nbd_probe import virtnbdbackup_socket_args
from .paths import backup_root, runtime_backup_path_ok, write_name_marker
from .run_records import CheckpointReadError, list_checkpoints, record_run
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .verify import verify
from .vms import VM, domain_state, is_safe_vm_name, is_safe_vm_uuid, list_vms

__all__ = [
    "backup_root",
    "backup_subpath_is_safe",
    "backup_vm",
    "current_month",
    "run_backups",
    "runtime_backup_path_ok",
    "timestamp",
    "verify",
]


def current_month(now: dt.datetime | None = None) -> str:
    # Calendar-month bucket keyed by wall-clock year+month: Dec 31 → YYYY-12,
    # Jan 1 → YYYY+1-01, no ISO-week year-boundary edge cases.
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
    # Second precision UTC. acquire_run_lock serializes whole runs; same-second
    # collisions only matter for new chain dir creation, where _prepare_dest
    # refuses overwrite rather than clobber prior data.
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S")


def backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _ensure_safe(config: Config, path: Path, message: str) -> bool:
    if backup_subpath_is_safe(config, path):
        return True
    event("error", message, path=str(path), backup_path=config.get("BACKUP_PATH"))
    return False


def _remove_partial_destination(dest: Path, vm_name: str) -> None:
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        event("error", "partial backup removal failed", vm=vm_name, destination=str(dest), error=str(exc))
        return
    if dest.exists():
        event("error", "partial backup removal incomplete", vm=vm_name, destination=str(dest))
    else:
        event("info", "removed partial backup", vm=vm_name, destination=str(dest))


def _confirm_inactive_marker_still_fresh(backup_dir: Path, vm: VM, month: str) -> bool:
    # Re-check that backup_dir exists so a concurrent operator removal between
    # marked_backup_dir() and here forces a recopy rather than "already fresh".
    try:
        if not backup_dir.is_dir():
            event("info", "inactive marker backup directory disappeared, recopying", vm=vm.name, path=str(backup_dir))
            return False
    except OSError as exc:
        event(
            "error", "inactive marker backup directory recheck failed", vm=vm.name, path=str(backup_dir), error=str(exc)
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
    if not _ensure_safe(config, month_dir, "inactive marker skipped because destination became unsafe"):
        return False
    # Re-check VM state after virtnbdbackup -l copy: backup_vm chose the copy
    # path from a pre-run state snapshot, so a VM started mid-copy would
    # otherwise be marked as a trusted full/copy when the data is live-state.
    post_state = domain_state(config, vm.name)
    if post_state is None or post_state.strip().lower() != "shut off":
        event("error", "inactive backup not trusted: VM is no longer shut off", vm=vm.name, post_state=post_state or "")
        return False
    post_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
    if post_fingerprint is None:
        event("error", "inactive fingerprint computation failed", vm=vm.name)
        return False
    if post_fingerprint != pre_fingerprint:
        event("error", "domain XML changed during inactive backup; backup not trusted", vm=vm.name)
        return False
    if not write_marker(inactive_marker, stamp, post_fingerprint, vm.name):
        return False
    # Backdate to pre-copy time so any mid- or post-copy disk write has a
    # newer mtime than the marker and forces a recopy.
    try:
        os.utime(inactive_marker, (pre_copy_time, pre_copy_time))
    except OSError as exc:
        event("error", "inactive marker backdate failed; rolling back marker", vm=vm.name, error=str(exc))
        remove_marker(inactive_marker, vm.name)
        return False
    remove_fingerprint(inactive_marker, vm.name)
    event("info", "backup completed", vm=vm.name, destination=str(dest))
    return True


def _maybe_reuse_inactive_backup(config: Config, vm: VM, month: str, month_dir: Path, marker: Path) -> bool:
    if not vm.inactive or config.enabled("INACTIVE_COPY_EVERY_RUN") or not marker_is_regular(marker):
        return False
    backup_dir = marked_backup_dir(config, month_dir, marker, vm.name)
    if backup_dir and inactive_marker_is_fresh(config.get("LIBVIRT_URI"), vm.name, marker):
        remove_fingerprint(marker, vm.name)  # reap legacy sidecar on the reuse path too
        return _confirm_inactive_marker_still_fresh(backup_dir, vm, month)
    if backup_dir:
        event("info", "inactive marker is stale, recopying", vm=vm.name, month=month)
    return False


def _attempt_partial_cleanup(config: Config, dest: Path, vm_name: str) -> None:
    if backup_subpath_is_safe(config, dest) and runtime_backup_path_ok(config):
        _remove_partial_destination(dest, vm_name)
    else:
        event("error", "partial backup removal skipped because destination is unsafe", vm=vm_name, path=str(dest))


def _virtnbdbackup_cmd(config: Config, vm: VM, level: str, dest: Path) -> list[str]:
    cmd = ["virtnbdbackup", "-U", config.get("LIBVIRT_URI"), "-d", vm.name, "-l", level, "-o", str(dest)]
    cmd.extend(virtnbdbackup_socket_args(config.get("LIBVIRT_URI"), vm.name))
    if config.enabled("BACKUP_COMPRESS"):
        cmd.append("--compress")
    return cmd


def _prepare_dest(config: Config, vm: VM, month_dir: Path, dest: Path, *, owns_chain_dir: bool) -> bool:
    if owns_chain_dir and dest.exists():
        # End-to-end-owned chain dir: refuse overwrite so a retry or backward
        # clock jump cannot delete prior data. Incrementals reuse the dir and
        # do not own it, so the guard only fires for new fulls/copies.
        event("error", "backup destination already exists", vm=vm.name, destination=str(dest))
        return False
    if not runtime_backup_path_ok(config):
        return False
    try:
        month_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        event(
            "error",
            "backup skipped because month directory creation failed",
            vm=vm.name,
            path=str(month_dir),
            error=str(exc),
        )
        return False
    return _ensure_safe(config, month_dir, "backup skipped because destination became unsafe")


def _run_virtnbdbackup(config: Config, vm: VM, cmd: list[str], dest: Path, *, owns_chain_dir: bool) -> bool:
    try:
        run_streamed(cmd)
    except CommandError as exc:
        # Incrementals reuse the chain dir; only end-to-end-owned dirs (new
        # full / inactive copy) are safe to clean on failure.
        if owns_chain_dir and dest.exists():
            _attempt_partial_cleanup(config, dest, vm.name)
        event("error", "backup failed", vm=vm.name, returncode=exc.result.returncode, stderr=exc.result.stderr.strip())
        return False
    if not dest.is_dir():
        event("error", "backup reported success but destination is missing", vm=vm.name, destination=str(dest))
        return False
    return _ensure_safe(config, dest, "backup destination became unsafe after virtnbdbackup")


def _backup_running(config: Config, vm: VM, month_dir: Path, stamp: str, marker: Path) -> bool:
    remove_marker(marker, vm.name)
    remove_fingerprint(marker, vm.name)
    pre_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
    if pre_fingerprint is None:
        event("error", "domain XML fingerprint computation failed", vm=vm.name)
        return False
    resolution: ChainResolution = resolve_chain(config, vm.name, month_dir, stamp, pre_fingerprint)
    if not _prepare_dest(config, vm, month_dir, resolution.chain_dir, owns_chain_dir=resolution.is_new_chain):
        return False
    cmd = _virtnbdbackup_cmd(config, vm, resolution.level, resolution.chain_dir)
    event(
        "info",
        "backup started",
        vm=vm.name,
        state=vm.state,
        backup_level=resolution.level,
        destination=str(resolution.chain_dir),
        chain_id=resolution.chain_dir.name,
    )
    try:
        checkpoints_before = list_checkpoints(resolution.chain_dir, vm.name)
    except CheckpointReadError as exc:
        # Chain dir exists but checkpoint metadata is unreadable: virtnbdbackup
        # would either compute the wrong diff (stale baseline) or succeed with
        # no recordable name. Fail before touching the chain.
        event(
            "error",
            "checkpoint metadata read failed",
            vm=vm.name,
            chain_dir=str(resolution.chain_dir),
            error=str(exc),
        )
        return False
    if not _run_virtnbdbackup(config, vm, cmd, resolution.chain_dir, owns_chain_dir=resolution.is_new_chain):
        return False
    if not record_run(resolution.chain_dir, stamp, checkpoints_before, vm.name):
        # Restore --at would silently fall back to chain end (newer state than
        # asked for) without a durable run record; fail loudly instead.
        event("error", "run record write failed; failing backup", vm=vm.name, chain_dir=str(resolution.chain_dir))
        return False
    write_name_marker(resolution.chain_dir, vm.name)
    if resolution.is_new_chain and not write_chain_state(
        month_dir, resolution.chain_dir.name, pre_fingerprint, vm.name
    ):
        return False
    # Re-check the NFS mount: a drop mid-run silently lands writes on the
    # underlying local directory.
    if not runtime_backup_path_ok(config):
        event("error", "backup completed but backup path no longer mounted", vm=vm.name)
        return False
    event("info", "backup completed", vm=vm.name, destination=str(resolution.chain_dir))
    return True


def _backup_inactive(config: Config, vm: VM, month_dir: Path, stamp: str, marker: Path) -> bool:
    dest = month_dir / stamp
    pre_fingerprint = domain_xml_fingerprint(config.get("LIBVIRT_URI"), vm.name)
    if pre_fingerprint is None:
        event("error", "inactive fingerprint computation failed", vm=vm.name)
        return False
    pre_copy_time = time.time()  # wall-clock; backward NTP step mid-copy is a tiny documented gap
    if not _prepare_dest(config, vm, month_dir, dest, owns_chain_dir=True):
        return False
    event("info", "backup started", vm=vm.name, state=vm.state, backup_level="copy", destination=str(dest))
    if not _run_virtnbdbackup(config, vm, _virtnbdbackup_cmd(config, vm, "copy", dest), dest, owns_chain_dir=True):
        return False
    write_name_marker(dest, vm.name)
    return _finalize_inactive_marker(config, marker, month_dir, vm, pre_fingerprint, stamp, dest, pre_copy_time)


def backup_vm(config: Config, vm: VM, month: str, stamp: str) -> bool:
    if not is_safe_vm_name(vm.name):
        raise ValueError(f"refusing unsafe VM name: {vm.name!r}")
    if not is_safe_vm_uuid(vm.uuid):
        raise ValueError(f"refusing unsafe VM uuid for {vm.name!r}: {vm.uuid!r}")
    if not runtime_backup_path_ok(config):
        return False
    month_dir = backup_root(config) / vm.uuid / month
    if not _ensure_safe(config, month_dir, "backup skipped because destination is unsafe"):
        return False
    inactive_marker = month_dir / ".inactive-copy-complete"
    if _maybe_reuse_inactive_backup(config, vm, month, month_dir, inactive_marker):
        return True
    if vm.inactive:
        return _backup_inactive(config, vm, month_dir, stamp, inactive_marker)
    return _backup_running(config, vm, month_dir, stamp, inactive_marker)


def run_backups(config: Config, *, month: str | None = None) -> int:
    # ``stamp`` is recomputed per VM: a single run-start stamp across a
    # minutes-to-hours sequential run would let ``restore --at`` pick a
    # backup taken well after the requested moment. Same-second collisions
    # are safe — ``_prepare_dest`` refuses overwrite on new-chain creation.
    month = month or current_month()
    ok = True
    for vm in list_vms(config):
        if not backup_vm(config, vm, month, timestamp()):
            ok = False
    return 0 if ok else 1
