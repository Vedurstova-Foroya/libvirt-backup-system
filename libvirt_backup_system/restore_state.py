from __future__ import annotations

from collections.abc import Callable

from .config import Config
from .logging_json import event
from .shell import CommandError, CommandResult, run

Runner = Callable[[list[str]], CommandResult]


def restore_vm_power(
    config: Config,
    vm_name: str,
    vm_state: str,
    *,
    runner: Runner | None = None,
) -> bool:
    if vm_state.strip().lower() != "running":
        return True
    try:
        (runner or run)(["virsh", "-c", config.get("LIBVIRT_URI"), "start", "--", vm_name])
    except CommandError as exc:
        event("error", "restored VM start failed", vm=vm_name, stderr=exc.result.stderr.strip())
        return False
    except OSError as exc:
        event("error", "virsh start unavailable", vm=vm_name, error=str(exc))
        return False
    event("info", "restored VM started", vm=vm_name)
    return True
