from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from . import kopia_repo
from .config import Config
from .consistency import UNKNOWN
from .kopia_client import as_string_keyed, as_string_string, build_config_args, run_kopia, tags_args
from .list_restore_points import rows_from_repo
from .logging_json import event
from .shell import CommandError


@dataclass(frozen=True)
class HostUsage:
    host_id: str
    repo_path: Path
    repo_bytes: int


@dataclass(frozen=True)
class VmUsage:
    host_id: str
    vm_uuid: str
    vm_name: str
    restore_points: int
    latest_logical_bytes: int
    backup_bytes: int
    latest_consistency: str = UNKNOWN


def _entry_size(path: Path) -> int:
    info = path.lstat()
    blocks = getattr(info, "st_blocks", 0)
    return int(blocks) * 512 if blocks else int(info.st_size)


def _repo_bytes(repo_path: Path) -> int:
    total = _entry_size(repo_path)
    for item in repo_path.rglob("*"):
        try:
            total += _entry_size(item)
        except FileNotFoundError:
            continue
    return total


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024:
            return f"{int(amount)} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} PiB"


def _format_table(headers: tuple[str, ...], rows: Sequence[tuple[str, ...]]) -> str:
    cells: list[tuple[str, ...]] = [headers, *rows]
    widths = [max(len(cell[i]) for cell in cells) for i in range(len(headers))]
    lines: list[str] = []
    for cell in cells:
        lines.append("  ".join(cell[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return "\n".join(lines)


def _host_repos(config: Config, host_id: str | None) -> list[kopia_repo.PeerRepo] | None:
    if host_id is not None:
        failure = kopia_repo.peer_host_id_failure(host_id)
        if failure is not None:
            event("error", "backup usage host_id rejected", host_id=host_id, reason=failure)
            return None
    try:
        repos = kopia_repo.discover_peer_repos(config)
    except kopia_repo.PeerDiscoveryError:
        return None
    if host_id is None:
        return repos
    selected = [repo for repo in repos if repo.host_id == host_id]
    if not selected:
        event("error", "backup host repo not found", host_id=host_id)
        return None
    return selected


def _connect_repo(config: Config, host_id: str) -> Path | None:
    if host_id == config.get("HOST_ID"):
        return kopia_repo.ensure_local_connected(config)
    return kopia_repo.ensure_peer_connected(config, host_id)


def _snapshot_logical_bytes(record: dict[str, object]) -> int:
    stats_total = as_string_keyed(record.get("stats")).get("totalSize")
    if isinstance(stats_total, int) and not isinstance(stats_total, bool) and stats_total >= 0:
        return stats_total
    return 0


def _snapshot_storage_bytes(record: dict[str, object]) -> int:
    storage = as_string_keyed(record.get("storageStats"))
    new_data = as_string_keyed(storage.get("newData"))
    packed_bytes = new_data.get("packedContentBytes")
    if isinstance(packed_bytes, int) and not isinstance(packed_bytes, bool) and packed_bytes >= 0:
        return packed_bytes
    return 0


def _disk_usage_from_repo(
    config: Config, *, host_id: str, config_file: Path, vm_uuid: str | None
) -> list[tuple[str, str, int, int]]:
    tags = {"kind": "disk"}
    if vm_uuid is not None:
        tags["vm-uuid"] = vm_uuid
    args = [
        *build_config_args(config_file),
        "snapshot",
        "list",
        "--all",
        "--json",
        "--show-identical",
        "--storage-stats",
        *tags_args(tags),
    ]
    result = run_kopia(
        args,
        password_file=kopia_repo.password_file_path(config),
        cache_dir=kopia_repo.cache_dir(config),
    )
    parsed: object = json.loads(result.stdout or "[]")
    if not isinstance(parsed, list):
        raise ValueError("kopia snapshot list returned a non-array JSON document")
    usage: list[tuple[str, str, int, int]] = []
    for raw in cast("list[object]", parsed):
        if not isinstance(raw, dict):
            continue
        record = as_string_keyed(cast("object", raw))
        raw_tags = {key.removeprefix("tag:"): value for key, value in as_string_string(record.get("tags")).items()}
        if raw_tags.get("kind") != "disk" or raw_tags.get("host") != host_id:
            continue
        snap_vm_uuid = raw_tags.get("vm-uuid", "")
        run_id = raw_tags.get("run-id", "")
        if not snap_vm_uuid or not run_id:
            continue
        if vm_uuid is not None and snap_vm_uuid != vm_uuid:
            continue
        usage.append((snap_vm_uuid, run_id, _snapshot_logical_bytes(record), _snapshot_storage_bytes(record)))
    return usage


def _vm_rows(config: Config, repos: list[kopia_repo.PeerRepo], vm_uuid: str | None) -> tuple[list[VmUsage], bool]:
    grouped: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
    names: dict[tuple[str, str], str] = {}
    latest_runs: dict[tuple[str, str], tuple[str, str, str]] = {}
    ok = True
    for repo in repos:
        config_file = _connect_repo(config, repo.host_id)
        if config_file is None:
            ok = False
            continue
        for row in rows_from_repo(config, host_id=repo.host_id, config_file=config_file):
            if vm_uuid is None or row.vm_uuid == vm_uuid:
                key = (repo.host_id, row.vm_uuid)
                names[key] = row.vm_name
                if key not in latest_runs or row.timestamp > latest_runs[key][0]:
                    latest_runs[key] = (row.timestamp, row.run_id, row.consistency)
        try:
            for snap_vm_uuid, _run_id, logical_bytes, backup_bytes in _disk_usage_from_repo(
                config, host_id=repo.host_id, config_file=config_file, vm_uuid=vm_uuid
            ):
                by_run = grouped.setdefault((repo.host_id, snap_vm_uuid), {})
                old_logical, old_backup = by_run.get(_run_id, (0, 0))
                by_run[_run_id] = (old_logical + logical_bytes, old_backup + backup_bytes)
        except (CommandError, json.JSONDecodeError, ValueError) as exc:
            detail = exc.result.stderr.strip() if isinstance(exc, CommandError) else str(exc)
            event("error", "kopia disk usage list failed", host_id=repo.host_id, error=detail)
            ok = False
    rows: list[VmUsage] = []
    for (host, uuid), by_run in grouped.items():
        latest = latest_runs.get((host, uuid))
        if latest is not None and latest[1] in by_run:
            latest_bytes = by_run[latest[1]][0]
        else:
            latest_bytes = max((values[0] for values in by_run.values()), default=0)
        backup_bytes = sum(values[1] for values in by_run.values())
        latest_consistency = latest[2] if latest is not None else UNKNOWN
        name = names.get((host, uuid), "")
        rows.append(VmUsage(host, uuid, name, len(by_run), latest_bytes, backup_bytes, latest_consistency))
    rows.sort(key=lambda row: (row.host_id, row.vm_name, row.vm_uuid))
    return rows, ok


def _print_host_usage(rows: list[HostUsage], *, json_output: bool) -> None:
    total = sum(row.repo_bytes for row in rows)
    if json_output:
        print(
            json.dumps(
                {
                    "hosts": [
                        {"host_id": row.host_id, "repo_bytes": row.repo_bytes, "repo_path": str(row.repo_path)}
                        for row in rows
                    ],
                    "mode": "hosts",
                    "total_repo_bytes": total,
                },
                sort_keys=True,
            )
        )
        return
    table_rows = [(row.host_id, str(row.repo_bytes), _human_bytes(row.repo_bytes), str(row.repo_path)) for row in rows]
    table_rows.append(("TOTAL", str(total), _human_bytes(total), ""))
    print(_format_table(("host-id", "repo-bytes", "human", "path"), table_rows))


def _print_vm_usage(rows: list[VmUsage], *, json_output: bool) -> None:
    total_disk, total_backup = sum(row.latest_logical_bytes for row in rows), sum(row.backup_bytes for row in rows)
    if json_output:
        print(
            json.dumps(
                {
                    "mode": "vms",
                    "total_backup_bytes": total_backup,
                    "total_latest_logical_bytes": total_disk,
                    "vms": [
                        {
                            "backup_bytes": row.backup_bytes,
                            "host_id": row.host_id,
                            "latest_logical_bytes": row.latest_logical_bytes,
                            "latest_consistency": row.latest_consistency,
                            "restore_point_count": row.restore_points,
                            "vm_name": row.vm_name,
                            "vm_uuid": row.vm_uuid,
                        }
                        for row in rows
                    ],
                },
                sort_keys=True,
            )
        )
        return
    table_rows = [
        (
            row.host_id,
            row.vm_uuid,
            row.vm_name or "-",
            str(row.restore_points),
            row.latest_consistency,
            str(row.latest_logical_bytes),
            _human_bytes(row.latest_logical_bytes),
            _human_bytes(row.backup_bytes),
        )
        for row in rows
    ]
    table_rows.append(
        (
            "TOTAL",
            "",
            "",
            str(sum(row.restore_points for row in rows)),
            "",
            str(total_disk),
            _human_bytes(total_disk),
            _human_bytes(total_backup),
        )
    )
    headers = (
        "host-id",
        "vm-uuid",
        "vm-name",
        "restore-points",
        "latest-consistency",
        "disk-bytes",
        "disk-size",
        "backup-size",
    )
    print(_format_table(headers, table_rows))


def backup_usage(
    config: Config, *, host_id: str | None = None, vm_uuid: str | None = None, json_output: bool = False
) -> int:
    repos = _host_repos(config, host_id)
    if repos is None:
        return 1
    if host_id is None and vm_uuid is None:
        host_rows = [HostUsage(repo.host_id, repo.repo_path, _repo_bytes(repo.repo_path)) for repo in repos]
        if host_rows or json_output:
            _print_host_usage(host_rows, json_output=json_output)
        else:
            event("info", "no backup repositories found")
        return 0
    vm_rows, ok = _vm_rows(config, repos, vm_uuid)
    if vm_rows or json_output:
        _print_vm_usage(vm_rows, json_output=json_output)
    else:
        event("info", "no matching backup usage found")
    return 0 if ok else 1
