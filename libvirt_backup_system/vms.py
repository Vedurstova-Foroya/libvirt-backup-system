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


def list_vms(config: Config, *, include_blacklisted: bool = False) -> list[VM]:
    result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "list", "--all", "--name"])
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    selected: list[VM] = []
    for name in names:
        if not include_blacklisted and name in config.blacklist:
            continue
        state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", name]).stdout.strip()
        selected.append(VM(name=name, state=state))
    return selected
