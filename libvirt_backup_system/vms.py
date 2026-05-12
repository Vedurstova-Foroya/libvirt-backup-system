from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .logging_json import event
from .shell import CommandError, run


@dataclass(frozen=True)
class VM:
    name: str
    state: str

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


def _assert_safe_vm_name(name: str) -> None:
    if not is_safe_vm_name(name):
        raise ValueError(f"refusing unsafe VM name: {name!r}")


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
        except CommandError as exc:
            # Propagate instead of skipping: a transient virsh failure for one
            # VM must not silently drop that VM from the run, since the missing
            # state would be reported as "no backup needed" rather than as an
            # error operators can act on.
            event(
                "error",
                "VM state discovery failed",
                vm=name,
                returncode=exc.result.returncode,
                stderr=exc.result.stderr.strip(),
            )
            raise
        selected.append(VM(name=name, state=state))
    return selected
