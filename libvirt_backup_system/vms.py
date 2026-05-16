from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .config import Config
from .logging_json import event
from .shell import CommandError, run

# Canonical libvirt domain UUID: 8-4-4-4-12 lowercase hex with dashes.
_UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _normalize_vm_name(name: str) -> str:
    # NFC-normalize VM names at the boundary so two libvirt domains whose
    # names differ only by Unicode normalization form (NFC vs NFD of the
    # same string) land in the *same* backup directory rather than being
    # silently treated as two unrelated VMs. The UUID-keyed layout protects
    # the on-disk dirs, but the in-process name comparison (blacklist,
    # verify --vm <name>) would otherwise diverge from operator expectations
    # when copy/pasted names round-trip through a clipboard that re-encodes.
    return unicodedata.normalize("NFC", name)


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


def domain_state(config: Config, vm_name: str) -> str | None:
    """Return the current libvirt ``domstate`` for ``vm_name``, or ``None`` on error.

    Used by inactive-backup finalize to re-verify the VM is still shut off
    after virtnbdbackup -l copy returns: if the VM was started mid-copy the
    copied data is live-state and must not be marked as a trusted backup.
    Returning ``None`` makes the caller fail closed rather than guess.
    """
    if not is_safe_vm_name(vm_name):
        return None
    try:
        return run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", vm_name]).stdout.strip()
    except CommandError as exc:
        event(
            "error",
            "VM state recheck failed",
            vm=vm_name,
            returncode=exc.result.returncode,
            stderr=exc.result.stderr.strip(),
        )
        return None


def resolve_vm_uuid(config: Config, vm_name: str) -> str | None:
    # Used by ``verify --vm <name>`` to translate an operator-supplied current
    # VM name into the UUID directory under which backups were written. Returns
    # ``None`` on any virsh failure (VM gone, libvirt unreachable, etc.) so
    # the caller can surface a clean "not found" rather than a fatal.
    vm_name = _normalize_vm_name(vm_name)
    if not is_safe_vm_name(vm_name):
        return None
    try:
        return _domuuid(config, vm_name)
    except (CommandError, ValueError) as exc:
        event("info", "VM name did not resolve to a UUID", vm=vm_name, error=str(exc))
        return None


def _list_name_uuid_pairs(config: Config) -> list[tuple[str, str]]:
    """Return ``(name, uuid)`` for every defined domain in one virsh call.

    ``virsh list --all --uuid --name`` prints tabular ``<uuid>  <name>`` rows.
    Doing this in one fork instead of one fork per VM cuts the discovery
    cost on a 100-VM host from ~200 forks to ~101.
    """
    result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "list", "--all", "--uuid", "--name"])
    pairs: list[tuple[str, str]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # virsh emits ``<uuid>   <name>`` separated by whitespace; the name
        # itself can contain spaces, so split off the leading UUID and keep
        # the remainder verbatim.
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        uuid = parts[0].strip().lower()
        name = _normalize_vm_name(parts[1].strip())
        pairs.append((name, uuid))
    return pairs


def list_vms(config: Config, *, include_blacklisted: bool = False) -> list[VM]:
    pairs = _list_name_uuid_pairs(config)
    selected: list[VM] = []
    # NFC-normalize the blacklist so an operator-typed entry that uses NFD
    # decomposition still matches the NFC-normalized name we got from virsh.
    blacklist = {_normalize_vm_name(item) for item in config.blacklist}
    for name, uuid in pairs:
        # Safety check runs before the blacklist filter so a maliciously-named
        # VM that also happens to be on the blacklist still surfaces as an
        # error rather than being silently dropped from the run.
        _assert_safe_vm_name(name)
        if not is_safe_vm_uuid(uuid):
            raise ValueError(f"virsh list returned an invalid UUID for {name!r}: {uuid!r}")
        if not include_blacklisted and name in blacklist:
            continue
        try:
            state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", name]).stdout.strip()
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
