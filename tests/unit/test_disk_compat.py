from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import disk_compat
from libvirt_backup_system.shell import CommandError, CommandResult
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


# --- Lines 18-20: list_disks raises CommandError/OSError/ValueError ---


class _ErrorSnapshotter:
    """Snapshotter that raises on list_disks."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def list_disks(self, _vm_name: str) -> list[DiskTarget]:
        raise self._exc


def test_selected_vm_list_disks_command_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    exc = CommandError(CommandResult(args=["virsh", "domblklist"], returncode=1, stdout="", stderr="no domain"))
    monkeypatch.setattr(disk_compat, "LibvirtSnapshotter", lambda *, libvirt_uri: _ErrorSnapshotter(exc))
    failures = disk_compat.selected_vm_disk_compatibility_failures(cfg, [_vm()])
    assert len(failures) == 1
    assert "unsupported backup disk for alpha" in failures[0]


def test_selected_vm_list_disks_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(
        disk_compat, "LibvirtSnapshotter", lambda *, libvirt_uri: _ErrorSnapshotter(OSError("cannot connect")),
    )
    failures = disk_compat.selected_vm_disk_compatibility_failures(cfg, [_vm()])
    assert len(failures) == 1
    assert "unsupported backup disk for alpha" in failures[0]
    assert "cannot connect" in failures[0]


# --- Lines 34-37: disk_image_info raises with stderr formatting ---


def test_disk_compatibility_failure_qemu_img_command_error(monkeypatch: pytest.MonkeyPatch) -> None:
    disk = DiskTarget("vda", Path("/img/alpha.qcow2"), "file")
    exc = CommandError(CommandResult(args=["qemu-img", "info"], returncode=1, stdout="", stderr="bad image"))
    monkeypatch.setattr(disk_compat, "disk_image_info", _raise(exc))
    result = disk_compat.disk_compatibility_failure("alpha", disk)
    assert result is not None
    assert "qemu-img info failed" in result
    assert "stderr=bad image" in result


def test_disk_compatibility_failure_qemu_img_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    disk = DiskTarget("vda", Path("/img/alpha.qcow2"), "file")
    monkeypatch.setattr(disk_compat, "disk_image_info", _raise(OSError("no such file")))
    result = disk_compat.disk_compatibility_failure("alpha", disk)
    assert result is not None
    assert "qemu-img info failed" in result
    assert "no such file" in result
    # OSError branch: no stderr field, so detail should NOT contain "stderr="
    assert "stderr=" not in result


# --- Line 42: missing virtual size ---


def test_disk_compatibility_failure_missing_virtual_size(monkeypatch: pytest.MonkeyPatch) -> None:
    disk = DiskTarget("vda", Path("/img/alpha.qcow2"), "file")
    monkeypatch.setattr(disk_compat, "disk_image_info", lambda _path: {"format": "qcow2"})
    result = disk_compat.disk_compatibility_failure("alpha", disk)
    assert result is not None
    assert "missing virtual size" in result


# --- Line 43: return None (happy path through disk_compatibility_failure) ---


def test_disk_compatibility_failure_returns_none_for_valid_qcow2(monkeypatch: pytest.MonkeyPatch) -> None:
    disk = DiskTarget("vda", Path("/img/alpha.qcow2"), "file")
    monkeypatch.setattr(disk_compat, "disk_image_info", lambda _path: {"format": "qcow2", "virtual-size": 1024})
    result = disk_compat.disk_compatibility_failure("alpha", disk)
    assert result is None


# --- helper ---


def _raise(exc: BaseException):
    def _inner(_path: str):
        raise exc
    return _inner
