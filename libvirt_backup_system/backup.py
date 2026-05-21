"""Kopia-engine backup orchestration.

Phase 3 cutover. The chain/run-records/fingerprint machinery is gone; a
backup run is now a sequence of kopia snapshots tagged with a per-VM
``run-id``. For each running VM, sequentially:

  1. ``vm_snapshot.freeze`` — external snapshots created on all disks.
  2. For each disk: ``stream_disk`` pipes the read-only base into
     ``kopia snapshot create --stdin-file=<target>.raw`` with disk tags.
  3. Write a per-run ``manifest.json`` and snapshot it as ``kind:meta``.
  4. ``vm_snapshot.commit`` — overlays folded back, original chain restored.

The orchestrator does NOT run kopia maintenance — that's a separate
systemd timer (see Phase 6) so a slow GC pass cannot block backups.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from . import kopia_repo, kopia_snapshots
from .config import Config
from .disks import vm_disk_paths_with_targets
from .logging_json import event
from .manifest import Manifest, ManifestDisk, snapshot_filename_for_target, utc_timestamp
from .paths import backup_root, runtime_backup_path_ok
from .preflight_estimate import disk_virtual_size_bytes
from .shell import CommandError
from .vm_snapshot import LibvirtSnapshotter, VmSnapshotter
from .vms import VM, is_safe_vm_name, is_safe_vm_uuid, list_vms

__all__ = [
    "backup_root",
    "backup_vm",
    "current_month",
    "run_backups",
    "runtime_backup_path_ok",
    "timestamp",
]


def current_month(now: dt.datetime | None = None) -> str:
    """Calendar bucket (``YYYY-MM``). Retained for log-line continuity."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def timestamp(now: dt.datetime | None = None) -> str:
    return utc_timestamp(now)


def _build_manifest(config: Config, vm: VM, run_id: str, stamp: str) -> Manifest:
    libvirt_uri = config.get("LIBVIRT_URI")
    disks = vm_disk_paths_with_targets(libvirt_uri, vm.name)
    manifest_disks: list[ManifestDisk] = []
    for entry in disks:
        try:
            virtual = disk_virtual_size_bytes(str(entry.source))
        except (CommandError, OSError, ValueError) as exc:
            event(
                "warning",
                "could not read virtual disk size; defaulting to 0",
                vm=vm.name,
                disk=str(entry.source),
                error=str(exc),
            )
            virtual = 0
        manifest_disks.append(
            ManifestDisk(
                target=entry.target,
                source_path=str(entry.source),
                virtual_size_bytes=virtual,
                snapshot_filename=snapshot_filename_for_target(entry.target),
            )
        )
    domain_xml = _read_domain_xml(libvirt_uri, vm.name)
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


def _read_domain_xml(libvirt_uri: str, vm_name: str) -> str:
    """Persistent ``virsh dumpxml`` for the manifest body.

    Kept lazy so unit tests can monkeypatch the helper without dragging the
    real virsh into every backup test.
    """
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


def _meta_tags(config: Config, vm: VM, run_id: str) -> dict[str, str]:
    return {
        "vm-uuid": vm.uuid,
        "kind": "meta",
        "host": config.get("HOST_ID"),
        "run-id": run_id,
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
    return f"{config.get('HOST_ID')}:libvirt-backup:{vm_uuid}/{suffix}"


@contextmanager
def _staging_dir() -> Iterator[Path]:
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
    snapper_obj: VmSnapshotter = snapper or LibvirtSnapshotter(libvirt_uri=config.get("LIBVIRT_URI"))
    run_id = str(uuid.uuid4())
    stamp = utc_timestamp()
    event("info", "backup started", vm=vm.name, run_id=run_id, timestamp=stamp)
    manifest = _build_manifest(config, vm, run_id, stamp)
    success, consistency = _stream_all_disks(config, vm, manifest, run_id, snapper_obj)
    if not success:
        return False
    if not _write_meta_snapshot(config, vm, manifest, run_id):
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
    config: Config, vm: VM, manifest: Manifest, run_id: str, snapper: VmSnapshotter
) -> tuple[bool, str]:
    """Drive freeze → stream each disk → commit, all through the same snapper.

    Returns ``(ok, consistency)`` where ``consistency`` is ``"quiesced"`` when
    QGA flushed the guest filesystems and ``"crash"`` when the freeze fell back
    to a crash-consistent snapshot. The flag is sourced from
    ``FrozenSnapshot.quiesced`` so operators can grep run logs to tell which
    VMs ran without QGA. ``commit`` is always invoked in the ``finally`` block
    so an overlay is never left wedged on the running domain even if a stream
    raises mid-flight.
    """
    snapper_disks = snapper.list_disks(vm.name)
    frozen = snapper.freeze(vm.name, snapper_disks)
    consistency = "quiesced" if frozen.quiesced else "crash"
    ok = True
    try:
        for disk in manifest.disks:
            base = next((d.source for d in snapper_disks if d.target == disk.target), None)
            if base is None:
                event("error", "missing snapshot base for disk", vm=vm.name, target=disk.target)
                ok = False
                break
            if not _stream_single_disk(config, vm, run_id, disk.target, base, snapper):
                ok = False
                break
    finally:
        try:
            snapper.commit(frozen)
        except CommandError as exc:
            event("error", "snapshot commit failed", vm=vm.name, stderr=exc.result.stderr.strip())
            ok = False
    return ok, consistency


def _stream_single_disk(
    config: Config, vm: VM, run_id: str, target: str, base: Path, snapper: VmSnapshotter
) -> bool:
    try:
        with snapper.stream_disk(base) as upstream:
            # The protocol pins ``stream_disk`` to ``AbstractContextManager[object]``
            # so a future Hyper-V provider isn't forced to yield a ``Popen`` —
            # the kopia stdin shim still wants the concrete type, so we narrow
            # at the boundary. Any snapper that yields something else has to
            # adapt to the same interface ``snapshot_create_stdin`` expects.
            kopia_snapshots.snapshot_create_stdin(
                config_file=kopia_repo.local_config_file(config),
                password_file=kopia_repo.password_file_path(config),
                cache_dir=kopia_repo.cache_dir(config),
                stdin_file=snapshot_filename_for_target(target),
                tags=_disk_tags(config, vm, run_id, target),
                source_stream=cast("subprocess.Popen[bytes] | None", upstream),
                override_source=_override_source(config, vm.uuid, target),
                parallelism=_parallelism(config),
            )
    except CommandError as exc:
        event(
            "error",
            "disk snapshot failed",
            vm=vm.name,
            target=target,
            stderr=exc.result.stderr.strip(),
        )
        return False
    return True


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
                tags=_meta_tags(config, vm, run_id),
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
