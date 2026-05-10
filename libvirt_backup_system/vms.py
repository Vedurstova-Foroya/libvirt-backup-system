from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .shell import run


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
    return bool(name) and name not in {".", ".."} and not name.startswith("-") and "/" not in name and "\\" not in name


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
        state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", name]).stdout.strip()
        selected.append(VM(name=name, state=state))
    return selected
