"""Enumerate every restorable kopia snapshot across all hosts and VMs.

Kopia migration: each host's repo lives under
``BACKUP_PATH/<host>/kopia-repo/``. We connect read-only to every peer repo
discovered there, list ``kind:meta`` snapshots (one per run), and emit one
table row per (host, vm-uuid, timestamp) triple. Copy the VM UUID and the
per-run timestamp columns straight into ``restore``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import kopia_repo, kopia_snapshots
from .config import Config
from .logging_json import event
from .paths import runtime_backup_path_ok
from .shell import CommandError


@dataclass(frozen=True)
class BackupRow:
    vm_uuid: str
    timestamp: str
    host_id: str
    vm_name: str
    run_id: str
    snapshot_id: str
    config_file: Path  # which kopia config-file points at this row's repo


def _local_rows(config: Config) -> list[BackupRow]:
    """List rows from this host's own repo (always present after install)."""
    cfg_file = kopia_repo.local_config_file(config)
    if not cfg_file.is_file():
        if not kopia_repo.local_repo_exists(config):
            return []
        if kopia_repo.ensure_local_repo(config, apply_global_policy=False) != 0:
            return []
    return _rows_from_repo(config, host_id=config.get("HOST_ID"), config_file=cfg_file)


def _peer_rows(config: Config) -> list[BackupRow]:
    rows: list[BackupRow] = []
    for peer in kopia_repo.iter_connected_peers(config):
        if peer.host_id == config.get("HOST_ID"):
            continue
        rows.extend(_rows_from_repo(config, host_id=peer.host_id, config_file=peer.config_file))
    return rows


def _rows_from_repo(config: Config, *, host_id: str, config_file: Path) -> list[BackupRow]:
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "meta"},
        )
    except CommandError as exc:
        event("error", "kopia snapshot list failed", host_id=host_id, stderr=exc.result.stderr.strip())
        return []
    except ValueError as exc:
        event("error", "kopia snapshot list returned bad data", host_id=host_id, error=str(exc))
        return []
    rows: list[BackupRow] = []
    for snap in snapshots:
        vm_uuid = snap.tags.get("vm-uuid", "")
        run_id = snap.tags.get("run-id", "")
        timestamp = snap.tags.get("timestamp", "")
        if not vm_uuid or not run_id or not timestamp:
            continue
        if snap.tags.get("host", "") != host_id:
            continue
        rows.append(
            BackupRow(
                vm_uuid=vm_uuid,
                timestamp=timestamp,
                host_id=host_id,
                vm_name=snap.tags.get("vm-name", ""),
                run_id=run_id,
                snapshot_id=snap.snapshot_id,
                config_file=config_file,
            )
        )
    return rows


def enumerate_backups(config: Config, *, vm_uuid: str | None = None) -> list[BackupRow]:
    rows = _local_rows(config) + _peer_rows(config)
    if vm_uuid is not None:
        rows = [row for row in rows if row.vm_uuid == vm_uuid]
    rows.sort(key=lambda row: row.timestamp, reverse=True)
    rows.sort(key=lambda row: (row.host_id, row.vm_uuid))
    return rows


_HEADERS = ("source-host-id", "vm-uuid", "vm-name", "timestamp", "run-id")


def format_rows(rows: list[BackupRow]) -> str:
    cells: list[tuple[str, ...]] = [_HEADERS]
    for row in rows:
        cells.append((row.host_id, row.vm_uuid, row.vm_name, row.timestamp, row.run_id))
    widths = [max(len(cell[i]) for cell in cells) for i in range(len(_HEADERS))]
    lines: list[str] = []
    for cell in cells:
        parts = [cell[i].ljust(widths[i]) for i in range(len(_HEADERS))]
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines)


def list_restore_points(config: Config) -> int:
    if not runtime_backup_path_ok(config):
        return 1
    rows = enumerate_backups(config)
    if not rows:
        event("info", "no backups found")
        return 0
    print(format_rows(rows))
    return 0
