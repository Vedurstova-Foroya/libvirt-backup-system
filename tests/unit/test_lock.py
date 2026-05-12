from __future__ import annotations

import os
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.lock import (
    LockBusyError,
    acquire_run_lock,
    lock_path,
)


def _config(tmp_path: Path) -> Config:
    return Config.load(prefix=str(tmp_path))


def test_acquire_run_lock_stamps_holder_pid(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with acquire_run_lock(cfg) as held:
        # Operators expect `cat run.lock` to surface the holding PID even while
        # the lock is still held; flock keeps mutual exclusion regardless.
        assert held.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_acquire_run_lock_tolerates_pid_write_failure(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)

    def fail_write(fd: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr("libvirt_backup_system.lock.os.write", fail_write)
    # PID stamping is diagnostic only; a write failure must not break locking.
    with acquire_run_lock(cfg) as held:
        assert held.exists()


def test_acquire_run_lock_creates_lockfile_and_releases(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    path = lock_path(cfg)
    assert not path.exists()
    with acquire_run_lock(cfg) as held:
        assert held == path
        assert held.exists()
    # Lock file remains; just unflocked. A subsequent acquire should succeed.
    with acquire_run_lock(cfg):
        pass


def test_acquire_run_lock_blocks_second_holder(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with acquire_run_lock(cfg) as path, pytest.raises(LockBusyError) as exc, acquire_run_lock(cfg):
        pass
    assert exc.value.path == path


def test_acquire_run_lock_releases_on_exception(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(RuntimeError, match="boom"), acquire_run_lock(cfg):
        raise RuntimeError("boom")
    # Lock should be free.
    with acquire_run_lock(cfg):
        pass
