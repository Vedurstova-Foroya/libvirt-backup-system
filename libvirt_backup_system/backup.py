"""Kopia-engine backup orchestration."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from . import backup_cleanup, kopia_repo, kopia_snapshots
from .config import Config, prefixed
from .logging_json import event
from .manifest import Manifest, ManifestDisk, snapshot_filename_for_target, utc_timestamp
from .paths import backup_root, runtime_backup_path_ok
from .preflight_estimate import disk_image_info
from .shell import CommandError
from .vm_snapshot import DiskTarget, LibvirtSnapshotter, VmSnapshotter
from .vms import VM, is_safe_vm_name, is_safe_vm_uuid, list_vms

__all__ = ["backup_root", "backup_vm", "run_backups", "runtime_backup_path_ok", "timestamp"]


def timestamp(now: dt.datetime | None = None) -> str:
    return utc_timestamp(now)


def _build_manifest(config: Config, vm: VM, run_id: str, stamp: str, disks: list[DiskTarget]) -> Manifest | None:
    libvirt_uri = config.get("LIBVIRT_URI")
    manifest_disks: list[ManifestDisk] = []
    for disk in disks:
        virtual = _validated_virtual_size(vm, disk)
        if virtual is None:
            return None
        manifest_disks.append(
            ManifestDisk(
                target=disk.target,
                source_path=str(disk.source),
                virtual_size_bytes=virtual,
                snapshot_filename=snapshot_filename_for_target(disk.target),
            )
        )
    try:
        domain_xml = _read_domain_xml(libvirt_uri, vm.name)
    except (CommandError, OSError) as exc:
        stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
        event("error", "domain xml read failed", vm=vm.name, error=str(exc), stderr=stderr)
        return None
    return Manifest(
        vm_name=vm.name,
        vm_uuid=vm.uuid,
        host_id=config.get("HOST_ID"),
        run_id=run_id,
        timestamp=stamp,
        libvirt_uri=libvirt_uri,
        domain_xml=domain_xml,
        disks=tuple(manifest_disks),
    )


def _validated_virtual_size(vm: VM, disk: DiskTarget) -> int | None:
    fields = {"vm": vm.name, "target": disk.target, "disk": str(disk.source)}
    if disk.source_type != "file" or str(disk.source) in {"", "-"}:
        event(
            "error",
            "unsupported backup disk",
            **fields,
            source_type=disk.source_type,
            reason="only file-backed qcow2 disks are supported",
        )
        return None
    try:
        info = disk_image_info(str(disk.source))
    except (CommandError, OSError, ValueError) as exc:
        stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
        event("error", "unsupported backup disk", **fields, error=str(exc), stderr=stderr)
        return None
    if info.get("format") != "qcow2":
        event(
            "error",
            "unsupported backup disk",
            **fields,
            disk_format=str(info.get("format")),
            reason="only qcow2 disks are supported",
        )
        return None
    virtual = info.get("virtual-size")
    if isinstance(virtual, bool) or not isinstance(virtual, int):
        event("error", "unsupported backup disk", **fields, reason="missing virtual size")
        return None
    return virtual


def _read_domain_xml(libvirt_uri: str, vm_name: str) -> str:
    from .shell import run as shell_run  # local to keep top of file lean

    result = shell_run(["virsh", "-c", libvirt_uri, "dumpxml", "--inactive", "--", vm_name])
    return result.stdout


def _disk_tags(config: Config, vm: VM, run_id: str, target: str) -> dict[str, str]:
    return {
        "vm-uuid": vm.uuid,
        "disk": target,
        "host": config.get("HOST_ID"),
        "run-id": run_id,
        "kind": "disk",
    }


def _meta_tags(config: Config, vm: VM, run_id: str, stamp: str) -> dict[str, str]:
    return {
        "vm-uuid": vm.uuid,
        "vm-name": vm.name,
        "kind": "meta",
        "host": config.get("HOST_ID"),
        "run-id": run_id,
        "timestamp": stamp,
    }


def _parallelism(config: Config) -> int | None:
    raw = config.get("KOPIA_PARALLELISM").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _override_source(config: Config, vm_uuid: str, suffix: str) -> str:
    return f"root@{config.get('HOST_ID')}:libvirt-backup:{vm_uuid}/{suffix}"


@contextmanager
def _staging_dir() -> Generator[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="lbs-meta-"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def backup_vm(config: Config, vm: VM, snapper: VmSnapshotter | None = None) -> bool:
    if not is_safe_vm_name(vm.name):
        raise ValueError(f"refusing unsafe VM name: {vm.name!r}")
    if not is_safe_vm_uuid(vm.uuid):
        raise ValueError(f"refusing unsafe VM uuid for {vm.name!r}: {vm.uuid!r}")
    if not runtime_backup_path_ok(config):
        return False
    snapper_obj: VmSnapshotter = snapper or LibvirtSnapshotter(
        config.get("LIBVIRT_URI"),
        socket_root=prefixed("/var/lib/libvirt/qemu", config.prefix),
        command_timeout_seconds=int(config.get("COMMAND_TIMEOUT_SECONDS")),
    )
    run_id = str(uuid.uuid4())
    stamp = utc_timestamp()
    event("info", "backup started", vm=vm.name, run_id=run_id, timestamp=stamp)
    try:
        snapper_disks = snapper_obj.list_disks(vm.name)
    except ValueError as exc:
        event("error", "unsupported backup disk", vm=vm.name, error=str(exc))
        return False
    except (CommandError, OSError) as exc:
        stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
        event("error", "disk listing failed", vm=vm.name, error=str(exc), stderr=stderr)
        return False
    manifest = _build_manifest(config, vm, run_id, stamp, snapper_disks)
    if manifest is None:
        return False
    success, consistency = _stream_all_disks(config, vm, manifest, run_id, snapper_obj, snapper_disks)
    if not success:
        return False
    if not runtime_backup_path_ok(config):
        event("error", "backup completed but backup path no longer mounted", vm=vm.name)
        return False
    event(
        "info",
        "backup completed",
        vm=vm.name,
        run_id=run_id,
        disks=len(manifest.disks),
        consistency=consistency,
    )
    return True


def _stream_all_disks(
    config: Config, vm: VM, manifest: Manifest, run_id: str, snapper: VmSnapshotter, snapper_disks: list[DiskTarget]
) -> tuple[bool, str]:
    """Drive freeze → stream each disk → commit, all through the same snapper."""
    try:
        frozen = snapper.freeze(vm.name, snapper_disks)
    except CommandError as exc:
        stderr = exc.result.stderr.strip()
        event("error", "snapshot freeze failed", vm=vm.name, run_id=run_id, stderr=stderr)
        return False, "failed"
    consistency = "quiesced" if frozen.quiesced else "crash"
    created_disk_snapshot_ids: list[str] = []
    meta_written = False
    ok = True
    try:
        for disk in manifest.disks:
            base = next((d.source for d in snapper_disks if d.target == disk.target), None)
            if base is None:
                event("error", "missing snapshot base for disk", vm=vm.name, target=disk.target)
                ok = False
                break
            snapshot_id = _stream_single_disk(config, vm, run_id, disk.target, base, snapper)
            if snapshot_id is None:
                ok = False
                break
            created_disk_snapshot_ids.append(snapshot_id)
        if ok:
            meta_written = _write_meta_snapshot(config, vm, manifest, run_id)
            ok = meta_written
    finally:
        if not meta_written and created_disk_snapshot_ids:
            backup_cleanup.cleanup_created_disk_snapshots(config, vm, run_id, created_disk_snapshot_ids)
        try:
            snapper.commit(frozen)
        except CommandError as exc:
            event("error", "snapshot commit failed", vm=vm.name, stderr=exc.result.stderr.strip())
            ok = False
    return ok, consistency


def _stream_single_disk(
    config: Config, vm: VM, run_id: str, target: str, base: Path, snapper: VmSnapshotter
) -> str | None:
    try:
        with snapper.stream_disk(base) as upstream:
            # The protocol pins ``stream_disk`` to ``AbstractContextManager[object]``
            # so a future Hyper-V provider isn't forced to yield a ``Popen`` —
            # the kopia stdin shim still wants the concrete type, so we narrow
            # at the boundary. Any snapper that yields something else has to
            # adapt to the same interface ``snapshot_create_stdin`` expects.
            return kopia_snapshots.snapshot_create_stdin(
                config_file=kopia_repo.local_config_file(config),
                password_file=kopia_repo.password_file_path(config),
                cache_dir=kopia_repo.cache_dir(config),
                stdin_file=snapshot_filename_for_target(target),
                tags=_disk_tags(config, vm, run_id, target),
                source_stream=cast("subprocess.Popen[bytes] | None", upstream),
                override_source=_override_source(config, vm.uuid, target),
                parallelism=_parallelism(config),
                timeout=int(config.get("COMMAND_TIMEOUT_SECONDS")),
            )
    except (CommandError, OSError, ValueError) as exc:
        if isinstance(exc, kopia_snapshots.SnapshotCreateError) and exc.snapshot_id is not None:
            backup_cleanup.cleanup_created_disk_snapshots(config, vm, run_id, [exc.snapshot_id])
        stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
        event("error", "disk snapshot failed", vm=vm.name, target=target, stderr=stderr, error=str(exc))
        return None


def _write_meta_snapshot(config: Config, vm: VM, manifest: Manifest, run_id: str) -> bool:
    with _staging_dir() as staging:
        if not manifest.write(staging, vm_name=vm.name):
            return False
        try:
            kopia_snapshots.snapshot_create_path(
                config_file=kopia_repo.local_config_file(config),
                password_file=kopia_repo.password_file_path(config),
                cache_dir=kopia_repo.cache_dir(config),
                path=staging,
                tags=_meta_tags(config, vm, run_id, manifest.timestamp),
                override_source=_override_source(config, vm.uuid, "meta"),
                parallelism=_parallelism(config),
            )
        except CommandError as exc:
            event(
                "error",
                "meta snapshot failed",
                vm=vm.name,
                run_id=run_id,
                stderr=exc.result.stderr.strip(),
            )
            return False
    return True


def run_backups(config: Config) -> int:
    ok = True
    for vm in list_vms(config):
        if not vm.running:
            event("info", "skipping vm because it is offline", vm=vm.name, state=vm.state)
            continue
        if not backup_vm(config, vm):
            ok = False
    return 0 if ok else 1
