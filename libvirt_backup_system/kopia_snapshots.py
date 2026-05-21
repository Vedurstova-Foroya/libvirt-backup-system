"""Snapshot operations against a connected kopia repo.

Pulled out of ``kopia_client.py`` to keep both modules under the project's
300-LOC ceiling. Shares the env / config-args / run helpers from
``kopia_client``; nothing here reaches outside the kopia surface.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .kopia_client import (
    KOPIA_BINARY,
    as_string_keyed,
    as_string_string,
    build_config_args,
    build_kopia_env,
    run_kopia,
    run_kopia_streamed,
    tags_args,
)
from .shell import CommandError, CommandResult


@dataclass(frozen=True)
class KopiaSnapshot:
    snapshot_id: str
    source_host: str
    source_user: str
    source_path: str
    start_time: str
    end_time: str
    tags: dict[str, str]
    root_entry_id: str

    @property
    def source(self) -> str:
        return f"{self.source_user}@{self.source_host}:{self.source_path}"


def _as_str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _snapshot_from_json(record: dict[str, object]) -> KopiaSnapshot | None:
    snap_id_raw = record.get("id")
    if not isinstance(snap_id_raw, str):
        return None
    source_map = as_string_keyed(record.get("source"))
    raw_host = source_map.get("host")
    raw_path = source_map.get("path")
    if not isinstance(raw_host, str) or not isinstance(raw_path, str):
        return None
    raw_user_value = source_map.get("userName")
    if not isinstance(raw_user_value, str):
        raw_user_value = source_map.get("user_name")
    raw_user = raw_user_value if isinstance(raw_user_value, str) else ""
    root_entry_map = as_string_keyed(record.get("rootEntry"))
    root_id_raw = root_entry_map.get("obj")
    root_id = root_id_raw if isinstance(root_id_raw, str) else ""
    return KopiaSnapshot(
        snapshot_id=snap_id_raw,
        source_host=raw_host,
        source_user=raw_user,
        source_path=raw_path,
        start_time=_as_str(record.get("startTime")),
        end_time=_as_str(record.get("endTime")),
        tags=as_string_string(record.get("tags")),
        root_entry_id=root_id,
    )


def snapshot_create_path(
    *,
    config_file: Path,
    password_file: Path,
    path: Path,
    tags: Mapping[str, str],
    override_source: str | None = None,
    parallelism: int | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Snapshot a path on disk, attaching ``tags``."""
    args = [*build_config_args(config_file), "snapshot", "create", str(path), *tags_args(tags)]
    if override_source is not None:
        args.extend(["--override-source", override_source])
    if parallelism is not None:
        args.extend(["--parallel", str(parallelism)])
    run_kopia_streamed(args, password_file=password_file, cache_dir=cache_dir)


def snapshot_create_stdin(
    *,
    config_file: Path,
    password_file: Path,
    stdin_file: str,
    tags: Mapping[str, str],
    source_stream: subprocess.Popen[bytes] | None,
    override_source: str,
    parallelism: int | None = None,
    cache_dir: Path | None = None,
) -> None:
    """Snapshot a byte stream piped on stdin as if it were ``stdin_file``.

    The upstream of the pipe (``qemu-nbd | nbdcopy``) sources the bytes
    through ``source_stream.stdout``. The caller is responsible for
    terminating the source process; we ``communicate`` to drain
    stdout/stderr and propagate failures.
    """
    env = build_kopia_env(password_file, cache_dir)
    args = [
        KOPIA_BINARY,
        *build_config_args(config_file),
        "snapshot",
        "create",
        f"--stdin-file={stdin_file}",
        f"--override-source={override_source}",
        *tags_args(tags),
        "-",
    ]
    if parallelism is not None:
        args.extend(["--parallel", str(parallelism)])
    source_stdout = source_stream.stdout if source_stream is not None else subprocess.PIPE
    kopia_proc = subprocess.Popen(
        args,
        stdin=source_stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    if source_stream is not None and source_stream.stdout is not None:
        with suppress(OSError):
            source_stream.stdout.close()
    stdout, stderr = kopia_proc.communicate()
    if source_stream is not None:
        source_stream.wait()
    if kopia_proc.returncode != 0:
        raise CommandError(
            CommandResult(args=args, returncode=kopia_proc.returncode, stdout=stdout or "", stderr=stderr or "")
        )
    if source_stream is not None and source_stream.returncode != 0:
        upstream_args: list[str] = []
        upstream_raw: object = source_stream.args
        if isinstance(upstream_raw, list | tuple):
            for arg in cast("list[object]", upstream_raw):
                upstream_args.append(str(arg))
        else:
            upstream_args.append(str(upstream_raw))
        raise CommandError(
            CommandResult(
                args=upstream_args,
                returncode=source_stream.returncode,
                stdout="",
                stderr=f"upstream of kopia stdin failed (rc={source_stream.returncode})",
            )
        )


def snapshot_list(
    *,
    config_file: Path,
    password_file: Path,
    tags: Mapping[str, str] | None = None,
    cache_dir: Path | None = None,
) -> list[KopiaSnapshot]:
    args = [*build_config_args(config_file), "snapshot", "list", "--all", "--json", "--show-identical"]
    if tags:
        args.extend(tags_args(tags))
    result = run_kopia(args, password_file=password_file, cache_dir=cache_dir)
    try:
        parsed: object = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"kopia snapshot list returned unparseable JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("kopia snapshot list returned a non-array JSON document")
    snapshots: list[KopiaSnapshot] = []
    for record in cast("list[object]", parsed):
        if not isinstance(record, dict):
            continue
        snap = _snapshot_from_json(as_string_keyed(cast("object", record)))
        if snap is None:
            continue
        if tags and not all(snap.tags.get(k) == v for k, v in tags.items()):
            continue
        snapshots.append(snap)
    return snapshots


def snapshot_restore_to_path(
    *,
    config_file: Path,
    password_file: Path,
    snapshot_id: str,
    dest: Path,
    cache_dir: Path | None = None,
) -> None:
    args = [*build_config_args(config_file), "snapshot", "restore", snapshot_id, str(dest)]
    run_kopia_streamed(args, password_file=password_file, cache_dir=cache_dir)


def snapshot_restore_to_stdout(
    *,
    config_file: Path,
    password_file: Path,
    snapshot_id: str,
    file_in_snapshot: str,
    cache_dir: Path | None = None,
) -> subprocess.Popen[bytes]:
    """Stream a file from a snapshot to a child process' stdout.

    Returns the kopia process; the caller pipes ``proc.stdout`` into the
    consumer (typically ``qemu-img convert``) and is responsible for
    ``wait()``-ing on the result.
    """
    env = build_kopia_env(password_file, cache_dir)
    spec = f"{snapshot_id}/{file_in_snapshot}"
    args = [KOPIA_BINARY, *build_config_args(config_file), "snapshot", "restore", spec, "-"]
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def snapshot_verify(
    *,
    config_file: Path,
    password_file: Path,
    cache_dir: Path | None = None,
    verify_files_percent: float | None = None,
    max_failures: int = 0,
    snapshot_ids: Iterable[str] | None = None,
) -> None:
    args = [*build_config_args(config_file), "snapshot", "verify", f"--max-failures={max_failures}"]
    if verify_files_percent is not None:
        args.append(f"--verify-files-percent={verify_files_percent}")
    if snapshot_ids:
        args.extend(snapshot_ids)
    run_kopia_streamed(args, password_file=password_file, cache_dir=cache_dir)
