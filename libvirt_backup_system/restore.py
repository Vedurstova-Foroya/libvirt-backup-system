from __future__ import annotations

import re
import shutil
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import stamp_is_safe
from .config import Config, prefixed
from .list_restore_points import BackupRow, enumerate_backups_result
from .logging_json import event
from .manifest import Manifest
from .paths import runtime_backup_path_ok
from .restore_define import RESTORED_CONFIG_FILE, define_restored_domain
from .restore_io import disk_snapshot_id as _disk_snapshot_id
from .restore_io import manifest_matches_request as _manifest_matches_request
from .restore_io import restore_manifest as _restore_manifest
from .restore_io import stream_disk_to_qcow2 as _stream_disk_to_qcow2
from .restore_overwrite import (
    cleanup_paths,
    overwrite_dest_map,
    overwrite_temp_dest_map,
    replace_overwrite_disks,  # noqa: F401 - re-exported for focused unit tests.  # pyright: ignore[reportUnusedImport]
    replace_overwrite_disks_with_backups,
    rollback_overwrite_disks,
)
from .restore_state import restore_vm_power
from .shell import CommandError, run
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid

RESTORE_STAGING_DIR = Path("/var/lib/libvirt-backup-system/restore")
RESTORE_TIMESTAMP_RE = re.compile(r"^\d{8}T\d{6}$")


@dataclass(frozen=True)
class _RestoreContext:
    row: BackupRow
    manifest: Manifest
    staging: Path
    verbose: bool


def _match_row(
    config: Config, vm_uuid: str, timestamp: str, host_id: str | None = None, run_id: str | None = None
) -> BackupRow | None:
    result = enumerate_backups_result(config, vm_uuid=vm_uuid)
    matches = [
        row
        for row in result.rows
        if row.timestamp == timestamp
        and (host_id is None or row.host_id == host_id)
        and (run_id is None or row.run_id == run_id)
    ]
    log_context = {"vm_uuid": vm_uuid, "timestamp": timestamp}
    if host_id is not None and host_id in result.failed_host_ids:
        event("error", "selected restore source repo unavailable", host_id=host_id, **log_context)
        return None
    if not result.ok and not result.failed_host_ids:
        event("error", "restore backup enumeration incomplete", **log_context)
        return None
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if not result.ok:
            event("error", "restore backup enumeration incomplete", **log_context)
        return None
    event("error", "restore timestamp matched multiple backups", **log_context)
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


def _local_domain_name_for_uuid(config: Config, vm_uuid: str) -> str | None:
    try:
        result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domname", "--", vm_uuid])
    except (CommandError, OSError):
        return None
    name = result.stdout.strip()
    return name or None


def _shutdown_and_undefine(config: Config, vm_name: str) -> bool:
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


def _inactive_domain_xml(config: Config, vm_name: str) -> str | None:
    try:
        return run(["virsh", "-c", config.get("LIBVIRT_URI"), "dumpxml", "--inactive", "--", vm_name]).stdout
    except CommandError as exc:
        event("error", "dumpxml failed before restore", vm=vm_name, stderr=exc.result.stderr.strip())
    except OSError as exc:
        event("error", "virsh dumpxml unavailable", vm=vm_name, error=str(exc))
    return None


def _define_domain_xml(config: Config, xml_path: Path, *, log_context: str) -> bool:
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "define", str(xml_path)])
    except CommandError as exc:
        event("error", f"{log_context} failed", stderr=exc.result.stderr.strip())
        return False
    except OSError as exc:
        event("error", f"{log_context} unavailable", error=str(exc))
        return False
    return True


def _materialize_disks(ctx: _RestoreContext, config: Config, dest_map: dict[str, Path]) -> bool:
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


def _turnkey_dest_map(manifest: Manifest, staging: Path) -> dict[str, Path]:
    return {disk.target: staging / _turnkey_disk_filename(disk.target) for disk in manifest.disks}


def _turnkey_disk_filename(target: str) -> str:
    safe_target = target.replace("/", "_")
    return f"{'disk' if safe_target in {'', '.', '..'} else safe_target}.qcow2"


def _rewrite_domain_disk_sources(domain_xml: str, dest_map: dict[str, Path]) -> str:
    """Rewrite restored file/block disk sources by ``<target dev=...>``."""
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


def _write_xml(ctx: _RestoreContext, filename: str, domain_xml: str) -> Path:
    path = ctx.staging / filename
    path.write_text(domain_xml, encoding="utf-8")
    return path


def _restore_overwrite(config: Config, ctx: _RestoreContext, vm_name: str) -> int:
    dest_map = overwrite_dest_map(ctx.manifest)
    temp_map = overwrite_temp_dest_map(dest_map)
    if not _materialize_disks(ctx, config, temp_map):
        cleanup_paths(temp_map)
        return 1
    original_xml = _inactive_domain_xml(config, vm_name)
    if original_xml is None:
        cleanup_paths(temp_map)
        return 1
    original_xml_path = _write_xml(ctx, "libvirt-backup-system-original.xml", original_xml)
    if not _shutdown_and_undefine(config, vm_name):
        cleanup_paths(temp_map)
        return 1
    backup_map = replace_overwrite_disks_with_backups(temp_map, dest_map)
    if backup_map is None:
        cleanup_paths(temp_map)
        _define_domain_xml(config, original_xml_path, log_context="original domain redefine")
        return 1
    xml_path = _write_xml(ctx, RESTORED_CONFIG_FILE, ctx.manifest.domain_xml)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, vm_name):
        rollback_overwrite_disks(backup_map, dest_map)
        _define_domain_xml(config, original_xml_path, log_context="original domain redefine")
        return 1
    cleanup_paths(backup_map)
    if not restore_vm_power(config, vm_name, ctx.manifest.vm_state, runner=run):
        return 1
    event("info", "restore overwrite completed", vm=vm_name, output=str(ctx.staging))
    return 0


def _restore_turnkey(config: Config, ctx: _RestoreContext) -> int:
    dest_map = _turnkey_dest_map(ctx.manifest, ctx.staging)
    if not _materialize_disks(ctx, config, dest_map):
        return 1
    rewritten = _rewrite_domain_disk_sources(ctx.manifest.domain_xml, dest_map)
    xml_path = _write_xml(ctx, RESTORED_CONFIG_FILE, rewritten)
    if not define_restored_domain(config, xml_path, ctx.manifest.vm_uuid, ctx.manifest.vm_name):
        return 1
    if not restore_vm_power(config, ctx.manifest.vm_name, ctx.manifest.vm_state, runner=run):
        return 1
    event(
        "info",
        "restore turnkey completed",
        vm_uuid=ctx.manifest.vm_uuid,
        host_id=ctx.row.host_id,
        output=str(ctx.staging),
    )
    return 0


def restore(
    config: Config,
    vm_uuid: str,
    timestamp: str,
    *,
    host_id: str | None = None,
    run_id: str | None = None,
    verbose: bool = True,
) -> int:
    if not is_safe_vm_uuid(vm_uuid):
        event("error", "restore vm_uuid is not a valid UUID", vm_uuid=vm_uuid)
        return 1
    if not stamp_is_safe(timestamp) or RESTORE_TIMESTAMP_RE.fullmatch(timestamp) is None:
        event("error", "restore timestamp is malformed", timestamp=timestamp)
        return 1
    if not runtime_backup_path_ok(config):
        return 1
    row = _match_row(config, vm_uuid, timestamp, host_id, run_id)
    if row is None:
        event("error", "restore found no backup matching uuid and timestamp", vm_uuid=vm_uuid, timestamp=timestamp)
        return 1
    if (staging_root := _ensure_staging_root(config)) is None:
        return 1
    if (staging := _prepare_staging(staging_root, vm_uuid, timestamp)) is None:
        return 1
    if (manifest := _restore_manifest(config, row, staging)) is None:
        return 1
    if not _manifest_matches_request(manifest, row, vm_uuid, timestamp):
        return 1
    if not is_safe_vm_name(manifest.vm_name):
        event("error", "manifest carries unsafe vm name", vm_name=manifest.vm_name)
        return 1
    ctx = _RestoreContext(row=row, manifest=manifest, staging=staging, verbose=verbose)
    if (
        row.host_id == config.get("HOST_ID")
        and (local_name := _local_domain_name_for_uuid(config, vm_uuid)) is not None
    ):
        return _restore_overwrite(config, ctx, local_name)
    return _restore_turnkey(config, ctx)
