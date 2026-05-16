from __future__ import annotations

import os
import signal
import subprocess
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from threading import Thread
from typing import IO

from .logging_json import event

STREAMED_TAIL_LINES = 256
TERMINATE_GRACE_SECONDS = 5.0
# Bound on how long run_streamed will block on stream-reader threads after
# proc.wait() returns. The leader can exit successfully while a grandchild
# inherits stdout/stderr and keeps the pipes open; without this cap the
# reader threads would block past COMMAND_TIMEOUT_SECONDS, outliving
# systemd's TimeoutStartSec=infinity and holding the run lock indefinitely.
# Kept shorter than TERMINATE_GRACE_SECONDS because the leader is already
# gone in this path — we are only waiting for a misbehaving grandchild.
STREAM_DRAIN_GRACE_SECONDS = 2.0
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
    # ``int(value)`` rejected operator-edited values like ``86400.0``; parse
    # through ``float`` first so any finite numeric form works.
    try:
        parsed = int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"command timeout must be a number: {value!r}") from exc
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
    # ``env=None`` inherits the parent process env. subprocess.run(timeout)
    # only signals the leader, so a fork+setsid grandchild would outlive the
    # run lock. Drive Popen by hand so timeout reaches the whole process
    # group, mirroring run_streamed's escalation.
    command = args[0] if args else ""
    timeout_seconds = _effective_timeout(timeout)
    proc = subprocess.Popen(
        args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True
    )
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        event("error", "command timed out", command=command, timeout_seconds=timeout_seconds)
        _kill_process_group(proc, pgid)
        try:
            stdout, stderr = proc.communicate(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr)
        result = CommandResult(args=args, returncode=TIMEOUT_RETURN_CODE, stdout=stdout or "", stderr=stderr or "")
        if check:
            raise CommandError(result) from exc
        return result
    except BaseException:
        _kill_process_group(proc, pgid)
        raise
    result = CommandResult(args=args, returncode=proc.returncode, stdout=stdout or "", stderr=stderr or "")
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
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip("\n")
            buffer.append(text)
            event(level, "command output", command=command, stream=stream_name, line=text)
    except (OSError, ValueError):
        # The parent may close the read end mid-read to break out of a wedge
        # (a grandchild holding the write end after the leader exited). Treat
        # that as a normal end-of-stream rather than letting the thread die
        # with an unhandled exception.
        pass
    with suppress(OSError, ValueError):
        stream.close()


def _kill_process_group(proc: subprocess.Popen[str], pgid: int) -> None:
    if proc.poll() is not None:
        return
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


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
    except (OSError, ProcessLookupError):
        # Group is empty or already gone — nothing left to signal.
        return False
    return True


def _drain_stream_threads(
    threads: list[Thread],
    pgid: int,
    streams: tuple[IO[str], ...],
    command: str,
) -> None:
    """Bound the wait on stream-reader threads after the leader exits.

    proc.wait() only tracks the leader. A grandchild that inherited stdout
    or stderr keeps the parent-side pipe read open until it exits, so
    thread.join(timeout=None) can block far past COMMAND_TIMEOUT_SECONDS.
    Because the systemd unit runs with TimeoutStartSec=infinity and the run
    lock is held for the entire run, a wedged grandchild could otherwise
    pin the lock forever. Escalate in three steps:

    1. Bounded join — fast path when the leader closed cleanly.
    2. SIGTERM/SIGKILL the captured process group — reaps inherited
       grandchildren that did not call setsid.
    3. Close the read end of the pipes from the parent — last resort for a
       grandchild that detached into its own session and ignored the kill.
    """
    for thread in threads:
        thread.join(timeout=STREAM_DRAIN_GRACE_SECONDS)
    if not any(thread.is_alive() for thread in threads):
        return
    event(
        "warning",
        "stream drain timed out after leader exit; signaling process group",
        command=command,
        grace_seconds=STREAM_DRAIN_GRACE_SECONDS,
    )
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not _signal_group(pgid, sig):
            break
        for thread in threads:
            thread.join(timeout=STREAM_DRAIN_GRACE_SECONDS)
        if not any(thread.is_alive() for thread in threads):
            return
    event(
        "warning",
        "process group kill did not release stream pipes; closing parent-side pipes",
        command=command,
    )
    for stream in streams:
        with suppress(OSError, ValueError):
            stream.close()
    for thread in threads:
        thread.join(timeout=STREAM_DRAIN_GRACE_SECONDS)
    if any(thread.is_alive() for thread in threads):
        # The reader threads are daemons, so leaving them attached does not
        # block process exit. We log and continue rather than hang the run.
        event("error", "stream reader threads still blocked after pipe close; abandoning", command=command)


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
    # Capture the pgid while proc.pid is still alive. After proc.wait() reaps
    # the leader the pid is gone, but the process group can still hold
    # grandchildren that need to be signaled to release inherited pipes.
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = proc.pid
    stdout_stream = proc.stdout
    stderr_stream = proc.stderr
    # Defensive: Popen always opens both streams when PIPE is requested.
    if stdout_stream is None or stderr_stream is None:  # pragma: no cover
        _kill_process_group(proc, pgid)
        raise RuntimeError("subprocess did not open both stdout and stderr")
    streams: tuple[IO[str], ...] = (stdout_stream, stderr_stream)
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
            event("error", "command timed out", command=command, timeout_seconds=timeout_seconds)
            _kill_process_group(proc, pgid)
            returncode = TIMEOUT_RETURN_CODE
        except BaseException:
            _kill_process_group(proc, pgid)
            raise
        finally:
            _drain_stream_threads(threads, pgid, streams, command)
    finally:
        for stream in streams:
            with suppress(OSError, ValueError):
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
