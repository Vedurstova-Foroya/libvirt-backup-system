from __future__ import annotations

import os
import signal
import subprocess
from contextlib import suppress
from typing import Any

from .shell import TERMINATE_GRACE_SECONDS


def popen_args(proc: subprocess.Popen[Any]) -> list[str]:
    raw = proc.args
    if isinstance(raw, list | tuple):
        return [str(arg) for arg in raw]
    return [str(raw)]


def timeout_message(command: str, timeout_seconds: float | None) -> str:
    if timeout_seconds is None:
        return f"{command} timed out"
    return f"{command} timed out after {timeout_seconds:g} seconds"


def terminate_process(proc: subprocess.Popen[Any] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    pgid = _process_group_id(proc)
    if pgid is None:
        with suppress(OSError):
            proc.terminate()
    else:
        _signal_group(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    if pgid is None:
        with suppress(OSError):
            proc.kill()
    else:
        _signal_group(pgid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)


def terminate_processes(*procs: subprocess.Popen[Any] | None) -> None:
    for proc in procs:
        terminate_process(proc)


def _process_group_id(proc: subprocess.Popen[Any]) -> int | None:
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _signal_group(pgid: int, sig: int) -> None:
    with suppress(OSError, ProcessLookupError):
        os.killpg(pgid, sig)
