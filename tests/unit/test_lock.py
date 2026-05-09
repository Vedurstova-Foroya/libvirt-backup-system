from __future__ import annotations

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
