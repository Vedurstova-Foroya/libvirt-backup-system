from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path

from .chains import ChainResolution, disable_chain_reuse, resolve_chain, write_chain_state
from .config import Config
from .disks import domain_xml_fingerprint
from .logging_json import event
from .nbd_probe import virtnbdbackup_socket_args
from .paths import backup_root, runtime_backup_path_ok
from .run_records import CheckpointReadError, list_checkpoints, record_run
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .verify import verify
from .vms import VM, is_safe_vm_name, is_safe_vm_uuid, list_vms

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
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
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
        # do not own it, so the guard only fires for new fulls.
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


def _run_virtnbdbackup(
    config: Config,
    vm: VM,
    cmd: list[str],
    dest: Path,
    *,
    owns_chain_dir: bool,
) -> bool:
    def _disable(state: str) -> None:
        if not dest.exists():
            return
        if owns_chain_dir:
            _attempt_partial_cleanup(config, dest, vm.name)
        else:
            disable_chain_reuse(dest.parent, dest, vm.name, f"virtnbdbackup {state} during incremental backup")

    try:
        run_streamed(cmd)
    except CommandError as exc:
        _disable("failed")
        event("error", "backup failed", vm=vm.name, returncode=exc.result.returncode, stderr=exc.result.stderr.strip())
        return False
    except BaseException:
        # run_streamed re-raises KeyboardInterrupt / SystemExit after killing the PG.
        _disable("interrupted")
        raise
    if not dest.is_dir():
        event("error", "backup reported success but destination is missing", vm=vm.name, destination=str(dest))
        return False
    return _ensure_safe(config, dest, "backup destination became unsafe after virtnbdbackup")


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
    if not record_run(resolution.chain_dir, stamp, checkpoints_before, vm.name, expect_new=True):
        try:
            dangling = sorted(list_checkpoints(resolution.chain_dir, vm.name) - checkpoints_before)
        except CheckpointReadError:
            dangling = []
        msg = "run record write failed; dangling checkpoints" if dangling else "run record write failed"
        event("error", msg, vm=vm.name, chain_dir=str(resolution.chain_dir))
        level = "new" if resolution.is_new_chain else "incremental"
        disable_chain_reuse(month_dir, resolution.chain_dir, vm.name, f"record_run failed after {level} backup")
        if resolution.is_new_chain:
            _attempt_partial_cleanup(config, resolution.chain_dir, vm.name)
        return False
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


def run_backups(config: Config, *, month: str | None = None) -> int:
    # ``stamp`` is recomputed per VM: a single run-start stamp across a
    # minutes-to-hours sequential run would let ``restore`` pick a backup taken
    # well after the requested moment. Same-second collisions are safe —
    # ``_prepare_dest`` refuses overwrite on new-chain creation.
    month = month or current_month()
    ok = True
    for vm in list_vms(config):
        if not vm.running:
            event("info", "skipping vm because it is offline", vm=vm.name, state=vm.state)
            continue
        if not backup_vm(config, vm, month, timestamp()):
            ok = False
    return 0 if ok else 1
