from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import disk_compat
from libvirt_backup_system.vm_snapshot import DiskTarget
from libvirt_backup_system.vms import VM

from ._preflight_helpers import make_config


class _FakeSnapshotter:
    def __init__(self, disks: list[DiskTarget]) -> None:
        self.disks = disks

    def list_disks(self, _vm_name: str) -> list[DiskTarget]:
        return self.disks


def _install_snapshotter(monkeypatch: pytest.MonkeyPatch, disks: list[DiskTarget]) -> None:
    monkeypatch.setattr(disk_compat, "LibvirtSnapshotter", lambda *, libvirt_uri: _FakeSnapshotter(disks))


def _vm() -> VM:
    return VM("alpha", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def test_selected_vm_disk_compatibility_accepts_file_qcow2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _install_snapshotter(monkeypatch, [DiskTarget("vda", Path("/img/alpha.qcow2"), "file")])
    monkeypatch.setattr(disk_compat, "disk_image_info", lambda _path: {"format": "qcow2", "virtual-size": 1})
    assert disk_compat.selected_vm_disk_compatibility_failures(cfg, [_vm()]) == []


def test_selected_vm_disk_compatibility_rejects_non_file_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _install_snapshotter(monkeypatch, [DiskTarget("vda", Path("/dev/vg/alpha"), "block")])
    failures = disk_compat.selected_vm_disk_compatibility_failures(cfg, [_vm()])
    assert failures and "only file-backed qcow2 disks are supported" in failures[0]


def test_selected_vm_disk_compatibility_rejects_non_qcow2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _install_snapshotter(monkeypatch, [DiskTarget("vda", Path("/img/alpha.raw"), "file")])
    monkeypatch.setattr(disk_compat, "disk_image_info", lambda _path: {"format": "raw", "virtual-size": 1})
    failures = disk_compat.selected_vm_disk_compatibility_failures(cfg, [_vm()])
    assert failures and "only qcow2 disks are supported" in failures[0]
