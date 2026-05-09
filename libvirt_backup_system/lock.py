from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from .config import Config, prefixed

LOCK_FILE_NAME = "run.lock"
STATE_DIR = "/var/lib/libvirt-backup-system"


class LockBusyError(RuntimeError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"another libvirt-backup-system run holds {path}")


def lock_path(config: Config) -> Path:
    return prefixed(STATE_DIR, config.prefix) / LOCK_FILE_NAME


@contextlib.contextmanager
def acquire_run_lock(config: Config) -> Iterator[Path]:
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusyError(path) from exc
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
