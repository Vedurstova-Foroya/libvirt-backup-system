from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from .config import Config, prefixed
from .logging_json import event

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
    # O_NOFOLLOW matches the openat pattern used by _write_probe and the
    # atomic_write helpers. /var/lib/libvirt-backup-system is root-owned so a
    # non-root symlink plant is not the threat model, but consistency keeps
    # future hardening from finding only one outlier.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockBusyError(path) from exc
        # Stamp the holder's PID so an operator paging in mid-run can see who
        # is holding the lock (cat run.lock). flock keeps mutual exclusion, so
        # a stamping failure does not affect correctness; log it anyway so
        # filesystem problems (full disk, frozen NFS) surface in the run logs
        # instead of as an unhelpful empty lock file.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        except OSError as exc:
            event("warning", "lock PID stamp write failed", path=str(path), error=str(exc))
        try:
            yield path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
