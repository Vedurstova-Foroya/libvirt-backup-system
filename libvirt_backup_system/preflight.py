from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import Config, bool_value, float_value, int_value
from .logging_json import event
from .shell import CommandError, run
from .vms import list_vms


REQUIRED_BINARIES = ["virsh", "virtnbdbackup", "virtnbdrestore", "qemu-img", "df"]


def _df_available_kb(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    result = run(["df", "-Pk", str(path)])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError("df output did not include a data row")
    parts = lines[-1].split()
    return int(parts[3])


def _remote_df_available_kb(config: Config) -> int:
    result = run(config.ssh_base + [config.remote_target, f"df -Pk {sh_quote(config.get('REMOTE_DIR'))}"])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError("remote df output did not include a data row")
    return int(lines[-1].split()[3])


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def check(config: Config) -> int:
    failures: list[str] = []
    for binary in REQUIRED_BINARIES:
        if not shutil.which(binary):
            failures.append(f"missing binary: {binary}")

    if config.enabled("REQUIRE_ROOT") and hasattr(os, "geteuid") and os.geteuid() != 0:
        failures.append("must run as root")

    try:
        vms = list_vms(config)
        if not vms:
            failures.append("no VMs selected")
    except Exception as exc:
        failures.append(f"libvirt VM discovery failed: {exc}")
        vms = []

    required_kb = int(
        len(vms)
        * float_value(config.values, "BACKUP_ESTIMATE_GB_PER_VM")
        * 1024
        * 1024
        * (1 + int_value(config.values, "SPACE_MARGIN_PERCENT") / 100)
    )
    try:
        available = _df_available_kb(config.path_value("LOCAL_ROOT"))
        if available < required_kb:
            failures.append(f"insufficient local space: available_kb={available} required_kb={required_kb}")
    except Exception as exc:
        failures.append(f"local space check failed: {exc}")

    if bool_value(config.get("REMOTE_ENABLED")):
        for key in ["REMOTE_HOST", "REMOTE_DIR"]:
            if not config.get(key):
                failures.append(f"{key} is required when REMOTE_ENABLED=true")
        if config.get("REMOTE_HOST") and config.get("REMOTE_DIR"):
            try:
                run(config.ssh_base + [config.remote_target, f"mkdir -p {sh_quote(config.get('REMOTE_DIR'))}"])
                available = _remote_df_available_kb(config)
                if available < required_kb:
                    failures.append(f"insufficient remote space: available_kb={available} required_kb={required_kb}")
            except CommandError as exc:
                failures.append(f"remote SSH check failed: {exc.result.stderr.strip() or exc.result.stdout.strip()}")
            except Exception as exc:
                failures.append(f"remote space check failed: {exc}")

    if failures:
        for failure in failures:
            event("error", "preflight failed", reason=failure)
        return 1
    event("info", "preflight passed", vm_count=len(vms), required_kb=required_kb)
    return 0
