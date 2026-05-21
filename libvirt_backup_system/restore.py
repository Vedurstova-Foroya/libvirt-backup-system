"""Restore a backup run identified by ``(vm-uuid, timestamp)``.

Kopia-engine restore: find the meta snapshot by tags, materialize the
manifest, then for each disk in the manifest pull the kopia disk snapshot
to a qcow2 file via ``qemu-img convert``. The ``overwrite`` path takes
over an existing local VM with the same UUID; the ``turnkey`` path defines
the VM fresh under ``RESTORE_STAGING_DIR``.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from . import kopia_repo, kopia_snapshots
from .atomic_io import stamp_is_safe
from .config import Config, prefixed
from .list_restore_points import BackupRow, enumerate_backups
from .logging_json import event
from .manifest import MANIFEST_FILENAME, Manifest, read_manifest
from .paths import runtime_backup_path_ok
from .restore_define import RESTORED_CONFIG_FILE, define_restored_domain
from .shell import CommandError, run
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid

RESTORE_STAGING_DIR = Path("/var/lib/libvirt-backup-system/restore")


@dataclass(frozen=True)
class _RestoreContext:
    row: BackupRow
    manifest: Manifest
    staging: Path
    verbose: bool


def _match_row(config: Config, vm_uuid: str, timestamp: str) -> BackupRow | None:
    for row in enumerate_backups(config, vm_uuid=vm_uuid):
        if row.timestamp == timestamp:
            return row
    return None


def _ensure_staging_root(config: Config) -> Path | None:
    root = prefixed(RESTORE_STAGING_DIR, config.prefix)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        event("error", "restore staging root creation failed", path=str(root), error=str(exc))
        return None
    return root


def _prepare_staging(root: Path, vm_uuid: str, timestamp: str) -> Path | None:
    staging = root / f"{vm_uuid}-{timestamp}"
    if not subpath_is_safe(root, staging):
        event("error", "restore staging path is unsafe", path=str(staging))
        return None
    with suppress(FileNotFoundError):
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        event("error", "restore staging dir creation failed", path=str(staging), error=str(exc))
        return None
    return staging


def _restore_manifest(config: Config, row: BackupRow, staging: Path) -> Manifest | None:
    meta_dir = staging / "_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    try:
        kopia_snapshots.snapshot_restore_to_path(
            config_file=row.config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            snapshot_id=row.snapshot_id,
            dest=meta_dir,
        )
    except CommandError as exc:
        event("error", "meta snapshot restore failed", stderr=exc.result.stderr.strip())
        return None
    try:
        return read_manifest(meta_dir / MANIFEST_FILENAME)
    except (OSError, ValueError) as exc:
        event("error", "manifest read failed", path=str(meta_dir / MANIFEST_FILENAME), error=str(exc))
        return None


def _disk_snapshot_id(config: Config, row: BackupRow, target: str) -> str | None:
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=row.config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "disk", "run-id": row.run_id, "disk": target},
        )
    except (CommandError, ValueError) as exc:
        event("error", "disk snapshot lookup failed", target=target, error=str(exc))
        return None
    if not snapshots:
        event("error", "disk snapshot missing for run", target=target, run_id=row.run_id)
        return None
    return snapshots[0].snapshot_id


def _stream_disk_to_qcow2(config: Config, row: BackupRow, snapshot_id: str, file_in_snap: str, dest: Path) -> bool:
    """Pipe a single kopia disk snapshot through ``qemu-img convert``.

    ``-O qcow2 -S 4096`` produces a sparse qcow2 so an all-zero source disk
    occupies only metadata on the destination.
    """
    kopia_proc = kopia_snapshots.snapshot_restore_to_stdout(
        config_file=row.config_file,
        password_file=kopia_repo.password_file_path(config),
        cache_dir=kopia_repo.cache_dir(config),
        snapshot_id=snapshot_id,
        file_in_snapshot=file_in_snap,
    )
    convert = subprocess.Popen(
        ["qemu-img", "convert", "-f", "raw", "-O", "qcow2", "-S", "4096", "-", str(dest)],
        stdin=kopia_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if kopia_proc.stdout is not None:
        with suppress(OSError):
            kopia_proc.stdout.close()
    stdout, stderr = convert.communicate()
    kopia_proc.wait()
    if convert.returncode != 0:
        event(
            "error",
            "qemu-img convert failed",
            target=dest.name,
            returncode=convert.returncode,
            stderr=(stderr or b"").decode("utf-8", errors="replace"),
        )
        return False
    if kopia_proc.returncode != 0:
        event("error", "kopia restore stream failed", target=dest.name, returncode=kopia_proc.returncode)
        return False
    _ = stdout
    return True


def _local_domain_name_for_uuid(config: Config, vm_uuid: str) -> str | None:
    try:
        result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domname", "--", vm_uuid])
    except (CommandError, OSError):
        return None
    name = result.stdout.strip()
    return name or None


def _shutdown_and_undefine(config: Config, vm_name: str) -> bool:
    """Tear down the existing local domain before overwriting its disks."""
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "destroy", "--", vm_name])
    except CommandError as exc:
        event("info", "destroy returned nonzero (likely already off)", vm=vm_name, stderr=exc.result.stderr.strip())
    except OSError as exc:
        event("error", "virsh destroy unavailable", vm=vm_name, error=str(exc))
        return False
    try:
        state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", vm_name]).stdout.strip()
    except (CommandError, OSError) as exc:
        event("error", "domstate check failed before restore", vm=vm_name, error=str(exc))
        return False
    if state.lower() != "shut off":
        event("error", "VM is not shut off; refusing to overwrite", vm=vm_name, state=state)
        return False
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "undefine", "--checkpoints-metadata", "--", vm_name])
    except CommandError as exc:
        event("error", "undefine failed", vm=vm_name, stderr=exc.result.stderr.strip())
        return False
    except OSError as exc:
        event("error", "virsh undefine unavailable", vm=vm_name, error=str(exc))
        return False
    return True


def _materialize_disks(ctx: _RestoreContext, config: Config, dest_dir: Path) -> bool:
    dest_dir.mkdir(parents=True, exist_ok=True)
    for disk in ctx.manifest.disks:
        snap_id = _disk_snapshot_id(config, ctx.row, disk.target)
        if snap_id is None:
            return False
        dest = dest_dir / f"{disk.target}.qcow2"
        if not _stream_disk_to_qcow2(config, ctx.row, snap_id, disk.snapshot_filename, dest):
            return False
        if ctx.verbose:
            event("info", "restored disk", target=disk.target, path=str(dest))
    return True


def _write_restored_xml(ctx: _RestoreContext) -> Path:
    """Persist the manifest's domain XML so ``define_restored_domain`` can read it."""
    path = ctx.staging / RESTORED_CONFIG_FILE
    path.write_text(ctx.manifest.domain_xml, encoding="utf-8")
    return path


def _restore_overwrite(config: Config, ctx: _RestoreContext, vm_name: str) -> int:
    if not _shutdown_and_undefine(config, vm_name):
        return 1
    dest_dir = _existing_disk_dir(ctx.manifest) or ctx.staging
    if not _materialize_disks(ctx, config, dest_dir):
        return 1
    xml_path = _write_restored_xml(ctx)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, vm_name):
        return 1
    event("info", "restore overwrite completed", vm=vm_name, output=str(dest_dir))
    return 0


def _restore_turnkey(config: Config, ctx: _RestoreContext) -> int:
    dest_dir = _existing_disk_dir(ctx.manifest) or ctx.staging
    if not _materialize_disks(ctx, config, dest_dir):
        return 1
    xml_path = _write_restored_xml(ctx)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, ctx.manifest.vm_name):
        return 1
    event(
        "info",
        "restore turnkey completed",
        vm_uuid=ctx.manifest.vm_uuid,
        host_id=ctx.row.host_id,
        output=str(dest_dir),
    )
    return 0


def _existing_disk_dir(manifest: Manifest) -> Path | None:
    parents = {Path(disk.source_path).parent for disk in manifest.disks}
    return next(iter(parents)) if len(parents) == 1 else None


def restore(config: Config, vm_uuid: str, timestamp: str, *, verbose: bool = True) -> int:
    if not is_safe_vm_uuid(vm_uuid):
        event("error", "restore vm_uuid is not a valid UUID", vm_uuid=vm_uuid)
        return 1
    if not stamp_is_safe(timestamp):
        event("error", "restore timestamp is malformed", timestamp=timestamp)
        return 1
    if not runtime_backup_path_ok(config):
        return 1
    row = _match_row(config, vm_uuid, timestamp)
    if row is None:
        event("error", "restore found no backup matching uuid and timestamp", vm_uuid=vm_uuid, timestamp=timestamp)
        return 1
    staging_root = _ensure_staging_root(config)
    if staging_root is None:
        return 1
    staging = _prepare_staging(staging_root, vm_uuid, timestamp)
    if staging is None:
        return 1
    manifest = _restore_manifest(config, row, staging)
    if manifest is None:
        return 1
    if not is_safe_vm_name(manifest.vm_name):
        event("error", "manifest carries unsafe vm name", vm_name=manifest.vm_name)
        return 1
    ctx = _RestoreContext(row=row, manifest=manifest, staging=staging, verbose=verbose)
    local_name = _local_domain_name_for_uuid(config, vm_uuid)
    same_host = row.host_id == config.get("HOST_ID")
    if same_host and local_name is not None:
        return _restore_overwrite(config, ctx, local_name)
    return _restore_turnkey(config, ctx)


# Kept as a thin entry point so cli.py can call the restore module without
# threading more state through.
__all__ = ["RESTORE_STAGING_DIR", "restore"]
