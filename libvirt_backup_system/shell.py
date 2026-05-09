from __future__ import annotations

import subprocess
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Thread
from typing import IO

from .logging_json import event

STREAMED_TAIL_LINES = 256


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
    with subprocess.Popen(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=1,
    ) as proc:
        stdout_stream = proc.stdout
        stderr_stream = proc.stderr
        # Defensive: Popen always opens both streams when PIPE is requested.
        if stdout_stream is None or stderr_stream is None:  # pragma: no cover
            raise RuntimeError("subprocess did not open both stdout and stderr")
        threads = [
            Thread(target=_tee_stream, args=(stdout_stream, "info", command, stdout_buf), daemon=True),
            Thread(target=_tee_stream, args=(stderr_stream, "error", command, stderr_buf), daemon=True),
        ]
        for thread in threads:
            thread.start()
        returncode = proc.wait()
        for thread in threads:
            thread.join()
    result = CommandResult(
        args=args,
        returncode=returncode,
        stdout="\n".join(stdout_buf),
        stderr="\n".join(stderr_buf),
    )
    if check and returncode != 0:
        raise CommandError(result)
    return result
