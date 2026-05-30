"""Enumerate every restorable kopia snapshot across all hosts and VMs.

Kopia migration: each host's repo lives under
``BACKUP_PATH/<host>/kopia-repo/``. We connect read-only to every peer repo
discovered there, list ``kind:meta`` snapshots (one per run), and emit one
table row per (host, vm-uuid, timestamp) triple. Copy the VM UUID and the
per-run timestamp columns straight into ``restore``.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class BackupEnumeration:
    rows: list[BackupRow]
    ok: bool


def _local_rows(config: Config) -> list[BackupRow]:
    """List rows from this host's own repo (always present after install)."""
    return _local_rows_result(config).rows


def _local_rows_result(config: Config) -> BackupEnumeration:
    cfg_file = kopia_repo.ensure_local_connected(config)
    if cfg_file is None:
        return BackupEnumeration([], ok=False)
    return _rows_from_repo_result(config, host_id=config.get("HOST_ID"), config_file=cfg_file)


def _peer_rows(config: Config) -> list[BackupRow]:
    return _peer_rows_result(config).rows


def _peer_rows_result(config: Config) -> BackupEnumeration:
    rows: list[BackupRow] = []
    ok = True
    try:
        peers = kopia_repo.discover_peer_repos(config)
    except kopia_repo.PeerDiscoveryError:
        return BackupEnumeration([], ok=False)
    for peer in peers:
        if peer.host_id == config.get("HOST_ID"):
            continue
        config_file = kopia_repo.ensure_peer_connected(config, peer.host_id)
        if config_file is None:
            ok = False
            continue
        result = _rows_from_repo_result(config, host_id=peer.host_id, config_file=config_file)
        rows.extend(result.rows)
        ok = ok and result.ok
    return BackupEnumeration(rows, ok)


def _rows_from_repo(config: Config, *, host_id: str, config_file: Path) -> list[BackupRow]:
    return _rows_from_repo_result(config, host_id=host_id, config_file=config_file).rows


def _rows_from_repo_result(config: Config, *, host_id: str, config_file: Path) -> BackupEnumeration:
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "meta"},
        )
    except CommandError as exc:
        event("error", "kopia snapshot list failed", host_id=host_id, stderr=exc.result.stderr.strip())
        return BackupEnumeration([], ok=False)
    except ValueError as exc:
        event("error", "kopia snapshot list returned bad data", host_id=host_id, error=str(exc))
        return BackupEnumeration([], ok=False)
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
    return BackupEnumeration(rows, ok=True)


def enumerate_backups(config: Config, *, vm_uuid: str | None = None) -> list[BackupRow]:
    return enumerate_backups_result(config, vm_uuid=vm_uuid).rows


def enumerate_backups_result(config: Config, *, vm_uuid: str | None = None) -> BackupEnumeration:
    local = _local_rows_result(config)
    peers = _peer_rows_result(config)
    rows = local.rows + peers.rows
    if vm_uuid is not None:
        rows = [row for row in rows if row.vm_uuid == vm_uuid]
    rows.sort(key=lambda row: row.timestamp, reverse=True)
    rows.sort(key=lambda row: (row.host_id, row.vm_uuid))
    return BackupEnumeration(rows, local.ok and peers.ok)


_HEADERS = ("source-host-id", "vm-uuid", "timestamp", "run-id", "vm-name")


def format_rows(rows: list[BackupRow]) -> str:
    cells: list[tuple[str, ...]] = [_HEADERS]
    for row in rows:
        cells.append((row.host_id, row.vm_uuid, row.timestamp, row.run_id, row.vm_name))
    widths = [max(len(cell[i]) for cell in cells) for i in range(len(_HEADERS))]
    lines: list[str] = []
    for cell in cells:
        parts = [cell[i].ljust(widths[i]) for i in range(len(_HEADERS))]
        lines.append("  ".join(parts).rstrip())
    return "\n".join(lines)


def format_json(rows: list[BackupRow]) -> str:
    payload = [
        {
            "run_id": row.run_id,
            "source_host_id": row.host_id,
            "timestamp": row.timestamp,
            "vm_name": row.vm_name,
            "vm_uuid": row.vm_uuid,
        }
        for row in rows
    ]
    return json.dumps(payload, sort_keys=True)


def list_restore_points(config: Config, *, json_output: bool = False) -> int:
    if not runtime_backup_path_ok(config):
        return 1
    result = enumerate_backups_result(config)
    if not result.ok:
        return 1
    rows = result.rows
    if not rows:
        if json_output:
            print("[]")
            return 0
        event("info", "no backups found")
        return 0
    print(format_json(rows) if json_output else format_rows(rows))
    return 0
