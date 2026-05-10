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
DEFAULT_COMMAND_TIMEOUT_SECONDS = 86400.0
TIMEOUT_RETURN_CODE = 124

_default_timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS


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


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def configure_default_timeout(value: str) -> None:
    global _default_timeout_seconds  # noqa: PLW0603
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("command timeout must be greater than 0")
    _default_timeout_seconds = parsed


def _effective_timeout(timeout: float | None) -> float | None:
    return _default_timeout_seconds if timeout is None else timeout


def run(
    args: list[str],
    *,
    check: bool = True,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> CommandResult:
    # ``env=None`` means subprocess inherits the parent process environment
    # in full. That is the intended behavior for libvirt-backup-system today
    # (no secrets pass through these shells), but callers handling sensitive
    # state must pass an explicit allowlist mapping instead of relying on
    # inheritance.
    try:
        proc = subprocess.run(
            args, text=True, capture_output=True, env=env, check=False, timeout=_effective_timeout(timeout)
        )
    except subprocess.TimeoutExpired as exc:
        command = args[0] if args else ""
        event("error", "command timed out", command=command, timeout_seconds=_effective_timeout(timeout))
        result = CommandResult(
            args=args,
            returncode=TIMEOUT_RETURN_CODE,
            stdout=_timeout_output(exc.stdout),
            stderr=_timeout_output(exc.stderr),
        )
        if check:
            raise CommandError(result) from exc
        return result
    result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(result)
    return result


def _tee_stream(
    stream: IO[str],
    level: str,
    stream_name: str,
    command: str,
    buffer: deque[str],
) -> None:
    for line in iter(stream.readline, ""):
        text = line.rstrip("\n")
        buffer.append(text)
        event(level, "command output", command=command, stream=stream_name, line=text)
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
    timeout: float | None = None,
) -> CommandResult:
    # See ``run`` above: ``env=None`` inherits the parent environment, including
    # systemd-injected variables. Callers handling secrets must pass an explicit
    # mapping rather than relying on inheritance.
    command = args[0] if args else ""
    timeout_seconds = _effective_timeout(timeout)
    stdout_buf: deque[str] = deque(maxlen=tail_lines)
    stderr_buf: deque[str] = deque(maxlen=tail_lines)
    timed_out = False
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
        Thread(target=_tee_stream, args=(stdout_stream, "info", "stdout", command, stdout_buf), daemon=True),
        Thread(target=_tee_stream, args=(stderr_stream, "info", "stderr", command, stderr_buf), daemon=True),
    ]
    try:
        for thread in threads:
            thread.start()
        try:
            returncode = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            event("error", "command timed out", command=command, timeout_seconds=timeout_seconds)
            _kill_process_group(proc)
            returncode = TIMEOUT_RETURN_CODE
        finally:
            for thread in threads:
                thread.join(timeout=TERMINATE_GRACE_SECONDS if timed_out else None)
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
        event("error", "command failed", command=command, returncode=returncode, stderr=result.stderr)
        raise CommandError(result)
    return result
