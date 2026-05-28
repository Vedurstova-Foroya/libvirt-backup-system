"""Read-only selected-VM disk compatibility checks."""

from __future__ import annotations

from .config import Config
from .preflight_estimate import disk_image_info
from .shell import CommandError
from .vm_snapshot import DiskTarget, LibvirtSnapshotter
from .vms import VM


def selected_vm_disk_compatibility_failures(config: Config, vms: list[VM]) -> list[str]:
    snapper = LibvirtSnapshotter(libvirt_uri=config.get("LIBVIRT_URI"))
    failures: list[str] = []
    for vm in vms:
        try:
            disks = snapper.list_disks(vm.name)
        except (CommandError, OSError, ValueError) as exc:
            failures.append(f"unsupported backup disk for {vm.name}: {exc}")
            continue
        for disk in disks:
            failure = disk_compatibility_failure(vm.name, disk)
            if failure is not None:
                failures.append(failure)
    return failures


def disk_compatibility_failure(vm_name: str, disk: DiskTarget) -> str | None:
    prefix = f"unsupported backup disk for {vm_name}:{disk.target}"
    if disk.source_type != "file" or str(disk.source) in {"", "-"}:
        return f"{prefix}: only file-backed qcow2 disks are supported"
    try:
        info = disk_image_info(str(disk.source))
    except (CommandError, OSError, ValueError) as exc:
        stderr = exc.result.stderr.strip() if isinstance(exc, CommandError) else ""
        detail = f"{exc}; stderr={stderr}" if stderr else str(exc)
        return f"{prefix}: qemu-img info failed for {disk.source}: {detail}"
    if info.get("format") != "qcow2":
        return f"{prefix}: only qcow2 disks are supported (format={info.get('format')!r})"
    virtual = info.get("virtual-size")
    if isinstance(virtual, bool) or not isinstance(virtual, int):
        return f"{prefix}: missing virtual size"
    return None
