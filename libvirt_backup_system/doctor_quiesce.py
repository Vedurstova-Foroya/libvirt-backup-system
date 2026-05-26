from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

from . import kopia_client
from .shell import CommandResult
from .systemd_units import RUN_UNIT_NAME

QUIESCE_FALLBACK_MESSAGE = "QGA quiesce failed; retrying without quiesce (crash-consistent)"


def check_recent_quiesce_fallbacks(
    root: Path,
    *,
    systemctl_available: Callable[[Path], bool],
    run_command: Callable[..., CommandResult],
) -> list[str]:
    """Surface QGA quiesce fallback warnings from the recent journal."""
    if not systemctl_available(root):
        return []
    result = run_command(
        [
            "journalctl",
            "-u",
            RUN_UNIT_NAME,
            "--since",
            "7 days ago",
            "--no-pager",
            "--output=cat",
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    return format_quiesce_fallbacks(collect_quiesce_fallbacks(result.stdout))


def collect_quiesce_fallbacks(stdout: str) -> dict[str, str]:
    """Return ``{vm: last_seen_ts}`` for each VM logging the fallback."""
    seen: dict[str, str] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or QUIESCE_FALLBACK_MESSAGE not in stripped:
            continue
        try:
            record_raw: object = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record_raw, dict):
            continue
        record = kopia_client.as_string_keyed(cast("object", record_raw))
        message = record.get("message")
        if message != QUIESCE_FALLBACK_MESSAGE:
            continue
        vm_raw = record.get("vm")
        ts_raw = record.get("ts")
        vm = vm_raw if isinstance(vm_raw, str) and vm_raw else "<unknown-vm>"
        ts = ts_raw if isinstance(ts_raw, str) and ts_raw else ""
        seen[vm] = ts
    return seen


def format_quiesce_fallbacks(events: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for vm in sorted(events):
        ts = events[vm]
        suffix = f" (last seen {ts})" if ts else ""
        findings.append(f"recent QGA quiesce fallback for {vm}{suffix}; install qemu-guest-agent inside the VM")
    return findings
