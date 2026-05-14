from __future__ import annotations

import contextlib
from pathlib import Path

from libvirt_backup_system.lock import LockBusyError
from libvirt_backup_system.preflight import check
from tests.unit.test_preflight import _preflight_config, patch_valid_preflight


def test_check_skips_nbd_probe_when_lock_busy(monkeypatch, capsys, backup_config) -> None:
    # check/doctor must not run the NBD probe while another libvirt-backup-system
    # process holds the run lock: the probe drives QEMU's HMP nbd_server_stop in
    # cleanup, which would tear down a live virtnbdbackup's NBD server.
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch, patch_probe=False)

    @contextlib.contextmanager
    def busy(_config: object):
        raise LockBusyError(Path("/tmp/fake.lock"))
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.acquire_run_lock", busy)
    probed: list[bool] = []
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.probe_qemu_socket_bind",
        lambda config, vms: probed.append(True) or [],
    )

    assert check(cfg) == 0
    assert probed == []
    assert "QEMU NBD socket bind probe skipped" in capsys.readouterr().out


def test_check_runs_nbd_probe_when_lock_held_by_caller(monkeypatch, backup_config) -> None:
    # The ``run`` path acquires the lock before calling check(..., lock_held=True)
    # so the probe runs directly without trying to re-acquire (which would
    # deadlock with the caller's hold).
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch, patch_probe=False)

    def must_not_acquire(_config: object) -> None:
        raise AssertionError("lock_held=True must skip nested lock acquisition")

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.acquire_run_lock", must_not_acquire)
    probed: list[bool] = []
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.probe_qemu_socket_bind",
        lambda config, vms: probed.append(True) or [],
    )

    assert check(cfg, lock_held=True) == 0
    assert probed == [True]
