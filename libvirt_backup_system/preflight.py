from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path

from .backup import backup_root
from .config import CONFIG_KEYS, Config, float_value, int_value
from .disks import vm_disk_paths
from .logging_json import event
from .shell import CommandError, run
from .storage import subpath_is_safe
from .vms import VM, list_vms

REQUIRED_BINARIES = ["virsh", "virtnbdbackup", "virtnbdrestore", "qemu-img", "df"]
BOOLEAN_KEYS = {"BACKUP_COMPRESS", "BACKUP_REQUIRE_NFS_MOUNT", "INACTIVE_COPY_EVERY_RUN", "REQUIRE_ROOT"}
INTEGER_KEYS = {"BACKUP_RETENTION_MONTHS", "SPACE_MARGIN_PERCENT"}
FLOAT_KEYS = {"BACKUP_ESTIMATE_GB_PER_VM", "BACKUP_INCREMENTAL_MULTIPLIER"}
ALLOWED_LIBVIRT_URI_PREFIXES = (
    "qemu:///",
    "qemu+ssh://",
    "qemu+tcp://",
    "qemu+tls://",
    "qemu+unix://",
    "test://",
    "test:///",
)


def validate_libvirt_uri(uri: str) -> bool:
    return uri.startswith(ALLOWED_LIBVIRT_URI_PREFIXES)


def _df_available_kb(path: Path) -> int:
    result = run(["df", "-Pk", str(path)])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError("df output did not include a data row")
    parts = lines[-1].split()
    return int(parts[3])


def _disk_virtual_size_bytes(path: str) -> int:
    result = run(["qemu-img", "info", "--output=json", "--", path])
    info = json.loads(result.stdout)
    return int(info["virtual-size"])


def _vm_estimated_bytes(uri: str, vm: VM, fallback_bytes: int) -> int:
    try:
        disks = vm_disk_paths(uri, vm.name)
    except (CommandError, OSError, ValueError) as exc:
        event("warning", "disk list failed for VM", vm=vm.name, error=str(exc))
        return fallback_bytes
    total = 0
    for disk in disks:
        try:
            total += _disk_virtual_size_bytes(disk)
        except (CommandError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            event("warning", "qemu-img info failed for disk", vm=vm.name, disk=disk, error=str(exc))
            return fallback_bytes
    return total or fallback_bytes


def _estimate_required_kb(config: Config, vms: list[VM]) -> int:
    try:
        fallback_per_vm_gb = float_value(config.values, "BACKUP_ESTIMATE_GB_PER_VM")
        multiplier = float_value(config.values, "BACKUP_INCREMENTAL_MULTIPLIER")
        margin = 1 + int_value(config.values, "SPACE_MARGIN_PERCENT") / 100
    except ValueError:
        return 0
    if not math.isfinite(fallback_per_vm_gb) or not math.isfinite(multiplier):
        return 0
    fallback_per_vm_bytes = int(fallback_per_vm_gb * 1024 * 1024 * 1024)
    uri = config.get("LIBVIRT_URI")
    total_bytes = 0
    for vm in vms:
        total_bytes += _vm_estimated_bytes(uri, vm, fallback_per_vm_bytes)
    return int(total_bytes * multiplier * margin / 1024)


def _validate_required_present(config: Config) -> list[str]:
    return [f"{key} must not be empty" for key in sorted(CONFIG_KEYS - {"VM_BLACKLIST"}) if not config.get(key).strip()]


def _validate_booleans(config: Config) -> list[str]:
    failures: list[str] = []
    for key in sorted(BOOLEAN_KEYS):
        value = config.get(key).strip().lower()
        if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            failures.append(f"{key} must be a boolean value")
    return failures


def _validate_integers(config: Config) -> list[str]:
    failures: list[str] = []
    for key in sorted(INTEGER_KEYS):
        try:
            int_config_value = int_value(config.values, key)
        except ValueError:
            failures.append(f"{key} must be an integer")
            continue
        if key == "BACKUP_RETENTION_MONTHS":
            if int_config_value == 0 or int_config_value < -1:
                failures.append("BACKUP_RETENTION_MONTHS must be -1 (keep all) or a positive integer")
        elif int_config_value < 0:
            failures.append(f"{key} must be greater than or equal to 0")
    return failures


def _validate_floats(config: Config) -> list[str]:
    failures: list[str] = []
    for key in sorted(FLOAT_KEYS):
        try:
            float_config_value = float_value(config.values, key)
        except ValueError:
            failures.append(f"{key} must be a number")
            continue
        if not math.isfinite(float_config_value):
            failures.append(f"{key} must be a finite number")
            continue
        if float_config_value < 0:
            failures.append(f"{key} must be greater than or equal to 0")
    return failures


def _validate_backup_path(config: Config) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        return []
    backup_path = config.path_value("BACKUP_PATH")
    if not backup_path.is_absolute():
        return ["BACKUP_PATH must be an absolute path"]
    if not backup_path.exists():
        return ["BACKUP_PATH must exist"]
    if not backup_path.is_dir():
        return ["BACKUP_PATH must be a directory"]
    if config.enabled("BACKUP_REQUIRE_NFS_MOUNT") and not backup_path.is_mount():
        return ["BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true"]
    if not subpath_is_safe(backup_path, backup_root(config)):
        return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
    try:
        host_root = backup_root(config)
        host_root.mkdir(parents=True, exist_ok=True)
        if not subpath_is_safe(backup_path, host_root):
            return ["BACKUP_PATH / HOST_ID must stay within BACKUP_PATH"]
        probe = host_root / ".libvirt-backup-system-write-test"
        try:
            probe.write_text("ok\n", encoding="utf-8")
        finally:
            probe.unlink(missing_ok=True)
    except OSError as exc:
        return [f"BACKUP_PATH must be writable: {exc}"]
    return []


def _validate_env_values(config: Config) -> list[str]:
    failures: list[str] = []
    failures.extend(_validate_required_present(config))
    failures.extend(_validate_booleans(config))
    failures.extend(_validate_integers(config))
    failures.extend(_validate_floats(config))
    libvirt_uri = config.get("LIBVIRT_URI").strip()
    if libvirt_uri and not validate_libvirt_uri(libvirt_uri):
        failures.append("LIBVIRT_URI must use one of these schemes: " + ", ".join(ALLOWED_LIBVIRT_URI_PREFIXES))
    failures.extend(_validate_backup_path(config))
    return failures


def validate_config(config: Config) -> int:
    failures = _validate_env_values(config)
    if failures:
        for failure in failures:
            event("error", "config validation failed", reason=failure)
        return 1
    return 0


def check(config: Config) -> int:
    failures = _validate_env_values(config)
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

    required_kb = _estimate_required_kb(config, vms)
    backup_path = config.path_value("BACKUP_PATH")
    if config.get("BACKUP_PATH").strip() and backup_path.exists() and backup_path.is_dir():
        try:
            available = _df_available_kb(backup_path)
            if available < required_kb:
                failures.append(f"insufficient backup space: available_kb={available} required_kb={required_kb}")
        except Exception as exc:
            failures.append(f"backup space check failed: {exc}")

    if failures:
        for failure in failures:
            event("error", "preflight failed", reason=failure)
        return 1
    event("info", "preflight passed", vm_count=len(vms), required_kb=required_kb)
    return 0
