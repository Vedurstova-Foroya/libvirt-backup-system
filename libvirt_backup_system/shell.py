from __future__ import annotations

import os
import signal
import subprocess
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Thread
from typing import IO

from .logging_json import event

STREAMED_TAIL_LINES = 256
TERMINATE_GRACE_SECONDS = 5.0


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult):
        self.result = result
        super().__init__(f"command failed ({result.returncode}): {' '.join(result.args)}")


def run(args: list[str], *, check: bool = True, env: Mapping[str, str] | None = None) -> CommandResult:
    proc = subprocess.run(args, text=True, capture_output=True, env=env, check=False)
    result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(result)
    return result


def _tee_stream(
    stream: IO[str],
    level: str,
    command: str,
    buffer: deque[str],
) -> None:
    for line in iter(stream.readline, ""):
        text = line.rstrip("\n")
        buffer.append(text)
        event(level, "command output", command=command, stream=level, line=text)
    stream.close()


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=TERMINATE_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def run_streamed(
    args: list[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    tail_lines: int = STREAMED_TAIL_LINES,
) -> CommandResult:
    command = args[0] if args else ""
    stdout_buf: deque[str] = deque(maxlen=tail_lines)
    stderr_buf: deque[str] = deque(maxlen=tail_lines)
    # start_new_session puts the child into its own process group so we can kill
    # the whole tree on a parent-side exception (e.g. KeyboardInterrupt) and not
    # leave virtnbdbackup or similar tools running with stale checkpoint state.
    proc = subprocess.Popen(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
        start_new_session=True,
    )
    stdout_stream = proc.stdout
    stderr_stream = proc.stderr
    # Defensive: Popen always opens both streams when PIPE is requested.
    if stdout_stream is None or stderr_stream is None:  # pragma: no cover
        _kill_process_group(proc)
        raise RuntimeError("subprocess did not open both stdout and stderr")
    threads: list[Thread] = [
        Thread(target=_tee_stream, args=(stdout_stream, "info", command, stdout_buf), daemon=True),
        Thread(target=_tee_stream, args=(stderr_stream, "error", command, stderr_buf), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        returncode = proc.wait()
        for thread in threads:
            thread.join()
    except BaseException:
        _kill_process_group(proc)
        for thread in threads:
            thread.join(timeout=TERMINATE_GRACE_SECONDS)
        raise
    finally:
        for stream in (stdout_stream, stderr_stream):
            stream.close()
    result = CommandResult(
        args=args,
        returncode=returncode,
        stdout="\n".join(stdout_buf),
        stderr="\n".join(stderr_buf),
    )
    if check and returncode != 0:
        raise CommandError(result)
    return result
