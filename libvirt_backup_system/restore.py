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
import xml.etree.ElementTree as ET
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
    matches = [row for row in enumerate_backups(config, vm_uuid=vm_uuid) if row.timestamp == timestamp]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    event("error", "restore timestamp matched multiple backups", vm_uuid=vm_uuid, timestamp=timestamp)
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


def _manifest_matches_request(manifest: Manifest, row: BackupRow, vm_uuid: str, timestamp: str) -> bool:
    expected = {
        "vm_uuid": vm_uuid,
        "timestamp": timestamp,
        "host_id": row.host_id,
        "run_id": row.run_id,
    }
    actual = {
        "vm_uuid": manifest.vm_uuid,
        "timestamp": manifest.timestamp,
        "host_id": manifest.host_id,
        "run_id": manifest.run_id,
    }
    mismatches = [key for key, value in expected.items() if actual[key] != value]
    if mismatches:
        event("error", "manifest does not match selected restore point", fields=",".join(mismatches))
        return False
    return True


def _disk_snapshot_id(config: Config, row: BackupRow, target: str) -> str | None:
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=row.config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "disk", "vm-uuid": row.vm_uuid, "run-id": row.run_id, "disk": target},
        )
    except (CommandError, ValueError) as exc:
        event("error", "disk snapshot lookup failed", target=target, error=str(exc))
        return None
    if not snapshots:
        event("error", "disk snapshot missing for run", target=target, run_id=row.run_id)
        return None
    if len(snapshots) > 1:
        event("error", "disk snapshot lookup matched multiple snapshots", target=target, run_id=row.run_id)
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


def _materialize_disks(ctx: _RestoreContext, config: Config, dest_map: dict[str, Path]) -> bool:
    """Restore each disk in the manifest to the path ``dest_map[disk.target]``.

    The caller chooses where each disk lands. For overwrite, that is the
    disk's original ``source_path``; for turnkey, a path under the staging
    dir. Any pre-existing file at ``dest`` is removed before
    ``qemu-img convert`` writes the new qcow2.
    """
    for disk in ctx.manifest.disks:
        dest = dest_map[disk.target]
        dest.parent.mkdir(parents=True, exist_ok=True)
        snap_id = _disk_snapshot_id(config, ctx.row, disk.target)
        if snap_id is None:
            return False
        with suppress(FileNotFoundError):
            dest.unlink()
        if not _stream_disk_to_qcow2(config, ctx.row, snap_id, disk.snapshot_filename, dest):
            return False
        if ctx.verbose:
            event("info", "restored disk", target=disk.target, path=str(dest))
    return True


def _overwrite_dest_map(manifest: Manifest) -> dict[str, Path]:
    """Per-disk destination paths for the overwrite branch: the original locations."""
    return {disk.target: Path(disk.source_path) for disk in manifest.disks}


def _turnkey_dest_map(manifest: Manifest, staging: Path) -> dict[str, Path]:
    """Per-disk destination paths for the turnkey branch: under ``staging``."""
    return {disk.target: staging / f"{disk.target}.qcow2" for disk in manifest.disks}


def _rewrite_domain_disk_sources(domain_xml: str, dest_map: dict[str, Path]) -> str:
    """Rewrite each ``<disk>/<source>`` to point at the restored qcow2.

    Mapping is by ``<target dev="..."/>``: the source under each ``<disk>``
    is rewritten to ``dest_map[target_dev]``. Disks whose target dev is not
    present in the map are left alone. Handles ``<source file=...>`` and
    ``<source dev=...>``; other source forms (volume / network) are skipped
    because the plan only restores file-backed qcow2 disks.
    """
    root = ET.fromstring(domain_xml)  # noqa: S314
    for disk_el in root.findall(".//devices/disk"):
        target_el = disk_el.find("target")
        if target_el is None:
            continue
        target_dev = target_el.get("dev")
        if target_dev is None or target_dev not in dest_map:
            continue
        source_el = disk_el.find("source")
        if source_el is None:
            continue
        new_path = str(dest_map[target_dev])
        if "file" in source_el.attrib:
            source_el.set("file", new_path)
        elif "dev" in source_el.attrib:
            source_el.set("dev", new_path)
    return ET.tostring(root, encoding="unicode")


def _write_restored_xml(ctx: _RestoreContext, domain_xml: str) -> Path:
    """Persist the (possibly rewritten) domain XML for ``define_restored_domain``."""
    path = ctx.staging / RESTORED_CONFIG_FILE
    path.write_text(domain_xml, encoding="utf-8")
    return path


def _restore_overwrite(config: Config, ctx: _RestoreContext, vm_name: str) -> int:
    if not _shutdown_and_undefine(config, vm_name):
        return 1
    dest_map = _overwrite_dest_map(ctx.manifest)
    if not _materialize_disks(ctx, config, dest_map):
        return 1
    # The manifest XML already references these original paths, so no
    # source-path rewrite is necessary; identity (name + uuid) is fixed up
    # downstream by ``define_restored_domain``.
    xml_path = _write_restored_xml(ctx, ctx.manifest.domain_xml)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, vm_name):
        return 1
    event("info", "restore overwrite completed", vm=vm_name, output=str(ctx.staging))
    return 0


def _restore_turnkey(config: Config, ctx: _RestoreContext) -> int:
    dest_map = _turnkey_dest_map(ctx.manifest, ctx.staging)
    if not _materialize_disks(ctx, config, dest_map):
        return 1
    rewritten = _rewrite_domain_disk_sources(ctx.manifest.domain_xml, dest_map)
    xml_path = _write_restored_xml(ctx, rewritten)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, ctx.manifest.vm_name):
        return 1
    event(
        "info",
        "restore turnkey completed",
        vm_uuid=ctx.manifest.vm_uuid,
        host_id=ctx.row.host_id,
        output=str(ctx.staging),
    )
    return 0


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
    if not _manifest_matches_request(manifest, row, vm_uuid, timestamp):
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
