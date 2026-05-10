from __future__ import annotations

import os
from pathlib import Path

import pytest

from libvirt_backup_system.disks import (
    disks_modified_after,
    domain_xml_fingerprint,
    inactive_marker_is_fresh,
    libvirt_uri_uses_remote_transport,
    vm_disk_paths,
)
from libvirt_backup_system.shell import CommandError, CommandResult


def _xml_result(xml: str) -> CommandResult:
    return CommandResult(["virsh"], 0, xml, "")


def _write_marker(marker: Path, stamp: str, fingerprint: str) -> None:
    marker.write_text(f"{stamp}\n{fingerprint}\n", encoding="utf-8")


def _stub_matching_fingerprint(monkeypatch, marker: Path, value: str = "deadbeef") -> None:
    existing = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else ["stamp"]
    stamp = existing[0] if existing else "stamp"
    _write_marker(marker, stamp, value)
    monkeypatch.setattr("libvirt_backup_system.disks.domain_xml_fingerprint", lambda uri, name: value)


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
      </devices>
    </domain>
    """
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))

    assert vm_disk_paths("qemu:///system", "alpha") == [
        "/var/lib/libvirt/images/has space.qcow2",
        "/dev/sdb",
    ]


def test_vm_disk_paths_raises_on_unsupported_disk_type(monkeypatch) -> None:
    xml = """
    <domain>
      <devices>
        <disk type='network' device='disk'>
          <source protocol='nbd'/>
        </disk>
      </devices>
    </domain>
    """
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    with pytest.raises(ValueError, match="unsupported disk type 'network'"):
        vm_disk_paths("qemu:///system", "alpha")


def test_inactive_marker_is_fresh_treats_unsupported_disk_as_stale(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    xml = "<domain><devices><disk type='volume' device='disk'><source pool='p' volume='v'/></disk></devices></domain>"
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_vm_disk_paths_raises_on_parse_error(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result("not xml"))
    with pytest.raises(ValueError, match="unparseable XML"):
        vm_disk_paths("qemu:///system", "alpha")


def test_inactive_marker_is_fresh_treats_parse_error_as_stale(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result("not xml"))
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_vm_disk_paths_raises_on_empty_source_value(monkeypatch) -> None:
    xml = "<domain><devices><disk type='file' device='disk'><source file=''/></disk></devices></domain>"
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    with pytest.raises(ValueError, match="missing disk source"):
        vm_disk_paths("qemu:///system", "alpha")


def test_inactive_marker_is_fresh_treats_missing_disk_source_as_stale(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    xml = "<domain><devices><disk type='file' device='disk'></disk></devices></domain>"
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


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


@pytest.mark.parametrize("uri", ["qemu+ssh://host/system", "qemu+tcp://host/system", "qemu+tls://host/system"])
def test_libvirt_uri_uses_remote_transport(uri: str) -> None:
    assert libvirt_uri_uses_remote_transport(uri)


def test_libvirt_uri_uses_remote_transport_rejects_local_uri() -> None:
    assert not libvirt_uri_uses_remote_transport("qemu:///system")
    assert not libvirt_uri_uses_remote_transport("qemu+unix:///system")


@pytest.mark.parametrize("uri", ["qemu+ssh://host/system", "qemu+tcp://host/system", "qemu+tls://host/system"])
def test_inactive_marker_is_fresh_treats_remote_uri_as_stale_without_disk_introspection(
    tmp_path: Path,
    monkeypatch,
    uri: str,
) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: pytest.fail("unexpected"))

    assert not inactive_marker_is_fresh(uri, "alpha", marker)


def test_inactive_marker_is_fresh_handles_introspection_failure(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr(
        "libvirt_backup_system.disks.vm_disk_paths",
        lambda uri, name: (_ for _ in ()).throw(CommandError(CommandResult([], 1, "", "no virsh"))),
    )
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_no_disks(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [])

    assert inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_recent_disk(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")
    _stub_matching_fingerprint(monkeypatch, marker)
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [str(disk)])

    older = marker.stat().st_mtime - 1000
    os.utime(marker, (older, older))
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_with_old_disk(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    disk = tmp_path / "a.qcow2"
    disk.write_bytes(b"data")
    _stub_matching_fingerprint(monkeypatch, marker)

    older = marker.stat().st_mtime - 1000
    os.utime(disk, (older, older))
    monkeypatch.setattr("libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: [str(disk)])

    assert inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_returns_false_when_fingerprint_line_missing(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    marker.write_text("ok\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.disks.domain_xml_fingerprint", lambda uri, name: "abcd")
    monkeypatch.setattr(
        "libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: pytest.fail("disks should not be queried")
    )
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_returns_false_when_fingerprint_changes(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    _write_marker(marker, "ok", "old")
    monkeypatch.setattr("libvirt_backup_system.disks.domain_xml_fingerprint", lambda uri, name: "new")
    monkeypatch.setattr(
        "libvirt_backup_system.disks.vm_disk_paths", lambda uri, name: pytest.fail("disks should not be queried")
    )
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_inactive_marker_is_fresh_returns_false_when_fingerprint_dumpxml_fails(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "marker"
    _write_marker(marker, "ok", "anything")
    monkeypatch.setattr(
        "libvirt_backup_system.disks.run",
        lambda args: (_ for _ in ()).throw(CommandError(CommandResult(args, 1, "", "boom"))),
    )
    assert not inactive_marker_is_fresh("qemu:///system", "alpha", marker)


def test_domain_xml_fingerprint_returns_none_when_virsh_missing(monkeypatch) -> None:
    def fake_run(args: list[str]) -> CommandResult:
        raise FileNotFoundError("virsh missing")

    monkeypatch.setattr("libvirt_backup_system.disks.run", fake_run)
    assert domain_xml_fingerprint("qemu:///system", "alpha") is None


def test_domain_xml_fingerprint_is_stable_for_same_xml(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.disks.run",
        lambda args: _xml_result("<domain><name>alpha</name></domain>"),
    )
    first = domain_xml_fingerprint("qemu:///system", "alpha")
    second = domain_xml_fingerprint("qemu:///system", "alpha")
    assert first is not None
    assert first == second


def test_domain_xml_fingerprint_returns_none_when_dumpxml_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.disks.run",
        lambda args: (_ for _ in ()).throw(CommandError(CommandResult(args, 1, "", "boom"))),
    )
    assert domain_xml_fingerprint("qemu:///system", "alpha") is None
