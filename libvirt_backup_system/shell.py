from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Mapping


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
    proc = subprocess.run(args, text=True, capture_output=True, env=env)
    result = CommandResult(args=args, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
    if check and proc.returncode != 0:
        raise CommandError(result)
    return result
