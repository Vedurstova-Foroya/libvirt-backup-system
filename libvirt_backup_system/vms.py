from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Config
from .logging_json import event
from .shell import CommandError, run

# Canonical libvirt domain UUID: 8-4-4-4-12 lowercase hex with dashes.
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@dataclass(frozen=True)
class VM:
    name: str
    state: str
    # ``uuid`` is the persistent libvirt identifier. Set by ``list_vms`` from
    # ``virsh domuuid``. Defaults to "" so unit-test fixtures that don't go
    # through ``list_vms`` can still build VM objects; ``backup_vm`` rejects
    # an empty/unsafe uuid before it touches the filesystem.
    uuid: str = ""

    @property
    def running(self) -> bool:
        return self.state.strip().lower() == "running"

    @property
    def inactive(self) -> bool:
        # Only fully shut-off VMs use the copy-level + monthly marker path.
        # Paused, in-shutdown, crashed, pmsuspended, etc. still have live state
        # and must not be misclassified as a cold offline backup.
        return self.state.strip().lower() == "shut off"


def is_safe_vm_name(name: str) -> bool:
    if not name or name in {".", ".."} or name.startswith("-"):
        return False
    if "/" in name or "\\" in name:
        return False
    # Reject control characters (incl. NUL, newline, tab, DEL) defensively.
    # Real libvirt rejects these at define-time, but a corrupted state file or
    # a hostile environment variable could still surface one here, and any
    # control char in a path argument flowing to virsh/virtnbdbackup is an
    # injection risk we want to refuse rather than pass through.
    return not any(ord(char) < 32 or ord(char) == 127 for char in name)


def is_safe_vm_uuid(uuid: str) -> bool:
    # Matches libvirt's canonical UUID format; rejects path separators,
    # control characters, and any string that could collide with a generic
    # directory name. The strict format check doubles as a sanity gate on
    # whatever virsh handed back.
    return bool(_UUID_PATTERN.fullmatch(uuid))


def _assert_safe_vm_name(name: str) -> None:
    if not is_safe_vm_name(name):
        raise ValueError(f"refusing unsafe VM name: {name!r}")


def _domuuid(config: Config, vm_name: str) -> str:
    result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domuuid", "--", vm_name])
    uuid = result.stdout.strip().lower()
    if not is_safe_vm_uuid(uuid):
        # Surface the offending value so a misbehaving virsh (custom build,
        # plugin, locale-mangled output) is diagnosable from the log alone.
        raise ValueError(f"virsh domuuid returned an invalid UUID for {vm_name!r}: {uuid!r}")
    return uuid


def resolve_vm_uuid(config: Config, vm_name: str) -> str | None:
    # Used by ``verify --vm <name>`` to translate an operator-supplied current
    # VM name into the UUID directory under which backups were written. Returns
    # ``None`` on any virsh failure (VM gone, libvirt unreachable, etc.) so
    # the caller can surface a clean "not found" rather than a fatal.
    if not is_safe_vm_name(vm_name):
        return None
    try:
        return _domuuid(config, vm_name)
    except (CommandError, ValueError) as exc:
        event("info", "VM name did not resolve to a UUID", vm=vm_name, error=str(exc))
        return None


def list_vms(config: Config, *, include_blacklisted: bool = False) -> list[VM]:
    result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "list", "--all", "--name"])
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    selected: list[VM] = []
    for name in names:
        if not include_blacklisted and name in config.blacklist:
            continue
        _assert_safe_vm_name(name)
        try:
            state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", name]).stdout.strip()
            uuid = _domuuid(config, name)
        except CommandError as exc:
            # Propagate instead of skipping: a transient virsh failure for one
            # VM must not silently drop that VM from the run, since the missing
            # state would be reported as "no backup needed" rather than as an
            # error operators can act on.
            event(
                "error",
                "VM discovery failed",
                vm=name,
                returncode=exc.result.returncode,
                stderr=exc.result.stderr.strip(),
            )
            raise
        selected.append(VM(name=name, state=state, uuid=uuid))
    return selected
