"""Kopia and qemu-img IO helpers for restore."""

from __future__ import annotations

import subprocess
from contextlib import suppress
from pathlib import Path

from . import kopia_repo, kopia_snapshots
from .config import Config
from .list_restore_points import BackupRow
from .logging_json import event
from .manifest import MANIFEST_FILENAME, Manifest, read_manifest
from .shell import CommandError


def restore_manifest(config: Config, row: BackupRow, staging: Path) -> Manifest | None:
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


def manifest_matches_request(manifest: Manifest, row: BackupRow, vm_uuid: str, timestamp: str) -> bool:
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


def disk_snapshot_id(config: Config, row: BackupRow, target: str) -> str | None:
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=row.config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "disk", "vm-uuid": row.vm_uuid, "run-id": row.run_id, "disk": target, "host": row.host_id},
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


def stream_disk_to_qcow2(config: Config, row: BackupRow, snapshot_id: str, file_in_snap: str, dest: Path) -> bool:
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
