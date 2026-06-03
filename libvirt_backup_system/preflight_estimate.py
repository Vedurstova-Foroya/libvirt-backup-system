from __future__ import annotations

import json
import math
from pathlib import Path
from typing import cast

from . import kopia_repo, kopia_snapshots
from .config import Config, float_value, int_value
from .disks import libvirt_uri_uses_remote_transport, vm_disk_paths
from .logging_json import event
from .shell import CommandError, run
from .vms import VM


def df_available_kb(path: Path) -> int:
    result = run(["df", "-Pk", "--", str(path)])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError("df output did not include a data row")
    parts = lines[-1].split()
    return int(parts[3])


def disk_image_info(path: str) -> dict[str, object]:
    result = run(["qemu-img", "info", "--output=json", "-U", "--", path])
    info = json.loads(result.stdout)
    if not isinstance(info, dict):
        raise ValueError("qemu-img info did not return a JSON object")
    return cast("dict[str, object]", info)


def disk_virtual_size_bytes(path: str) -> int:
    info = disk_image_info(path)
    raw = info["virtual-size"]
    if not isinstance(raw, int | float | str):
        raise TypeError(f"unexpected virtual-size type: {type(raw)}")
    return int(raw)


def vm_estimated_bytes(uri: str, vm: VM, fallback_bytes: int) -> int:
    if libvirt_uri_uses_remote_transport(uri):
        event("warning", "skipping local disk introspection for remote URI", vm=vm.name, uri=uri)
        return fallback_bytes
    try:
        disks = vm_disk_paths(uri, vm.name)
    except (CommandError, OSError, ValueError) as exc:
        event("warning", "disk list failed for VM", vm=vm.name, error=str(exc))
        return fallback_bytes
    total = 0
    for disk in disks:
        try:
            total += disk_virtual_size_bytes(disk)
        except (CommandError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
            event("warning", "qemu-img info failed for disk", vm=vm.name, disk=disk, error=str(exc), stderr=stderr)
            total += fallback_bytes
    return total or fallback_bytes


def estimate_required_kb(config: Config, vms: list[VM]) -> int:
    estimated_vms = _vms_needing_first_backup_estimate(config, vms)
    if not estimated_vms:
        return 0
    try:
        fallback_per_vm_gb = float_value(config.values, "BACKUP_ESTIMATE_GB_PER_VM")
        margin = 1 + int_value(config.values, "SPACE_MARGIN_PERCENT") / 100
    except ValueError:
        return 0
    if not math.isfinite(fallback_per_vm_gb):
        return 0
    fallback_per_vm_bytes = int(fallback_per_vm_gb * 1024 * 1024 * 1024)
    uri = config.get("LIBVIRT_URI")
    total_bytes = sum(vm_estimated_bytes(uri, vm, fallback_per_vm_bytes) for vm in estimated_vms)
    return int(total_bytes * margin / 1024)


def _vms_needing_first_backup_estimate(config: Config, vms: list[VM]) -> list[VM]:
    if not vms or not kopia_repo.local_repo_exists(config):
        return vms
    config_file = kopia_repo.ensure_local_connected(config)
    if config_file is None:
        return []
    try:
        snapshots = kopia_snapshots.snapshot_list(
            config_file=config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            tags={"kind": "meta"},
        )
    except (CommandError, OSError, ValueError) as exc:
        event("warning", "meta snapshot probe failed for space estimate", error=str(exc))
        return []
    seen_vm_uuids = {snap.tags.get("vm-uuid", "") for snap in snapshots}
    return [vm for vm in vms if vm.uuid not in seen_vm_uuids]
