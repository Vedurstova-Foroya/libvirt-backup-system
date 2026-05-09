from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.disks import (
    disks_modified_after,
    inactive_marker_is_fresh,
    vm_disk_paths,
)
from libvirt_backup_system.shell import CommandError, CommandResult


def _xml_result(xml: str) -> CommandResult:
    return CommandResult(["virsh"], 0, xml, "")


def test_vm_disk_paths_parses_dumpxml(monkeypatch) -> None:
    xml = """
    <domain>
      <devices>
        <disk type='file' device='disk'>
          <source file='/var/lib/libvirt/images/has space.qcow2'/>
        </disk>
        <disk type='block' device='disk'>
          <source dev='/dev/sdb'/>
        </disk>
        <disk type='file' device='cdrom'>
          <source file='/iso/install.iso'/>
        </disk>
        <disk type='network' device='disk'>
          <source protocol='nbd'/>
        </disk>
        <disk type='file' device='disk'>
        </disk>
      </devices>
    </domain>
    """
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))

    assert vm_disk_paths("qemu:///system", "alpha") == [
        "/var/lib/libvirt/images/has space.qcow2",
        "/dev/sdb",
    ]


def test_vm_disk_paths_returns_empty_on_parse_error(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result("not xml"))
    assert vm_disk_paths("qemu:///system", "alpha") == []


def test_vm_disk_paths_skips_empty_source_value(monkeypatch) -> None:
    xml = "<domain><devices><disk type='file' device='disk'><source file=''/></disk></devices></domain>"
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    assert vm_disk_paths("qemu:///system", "alpha") == []


def test_disks_modified_after_detects_changes(tmp_path: Path) -> None:
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")
    mtime = disk.stat().st_mtime

    assert not disks_modified_after([str(disk)], mtime)

    later = mtime + 100
    disk.write_bytes(b"more")
    os.utime(disk, (later, later))
    assert disks_modified_after([str(disk)], mtime)


def test_disks_modified_after_treats_block_device_as_modified(monkeypatch, tmp_path: Path) -> None:
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")
    monkeypatch.setattr("libvirt_backup_system.disks.Path.is_block_device", lambda self: True)

    assert disks_modified_after([str(disk)], 1.0)


def test_disks_modified_after_treats_oserror_as_modified(tmp_path: Path) -> None:
    missing = tmp_path / "missing.qcow2"
    assert disks_modified_after([str(missing)], 1.0)


def test_inactive_marker_is_fresh_missing_marker(tmp_path: Path) -> None:
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", tmp_path / "missing")


def test_inactive_marker_is_fresh_handles_introspection_failure(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr(
        "libvirt_backup_system.disks.vm_disk_paths",
        lambda uri, name: (_ for _ in ()).throw(CommandError(CommandResult([], 1, "", "no virsh"))),
    )
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_no_disks(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [])

    assert inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_recent_disk(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [str(disk)])

    older = marker.stat().st_mtime - 1000
    os.utime(marker, (older, older))
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_old_disk(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")

    older = marker.stat().st_mtime - 1000
    os.utime(disk, (older, older))
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [str(disk)])

    assert inactive_marker_is_fresh("qemu:///system", "alpha", marker)
