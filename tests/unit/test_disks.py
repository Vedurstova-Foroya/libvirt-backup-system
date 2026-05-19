from __future__ import annotations

import pytest

from libvirt_backup_system.disks import (
    domain_xml_fingerprint,
    libvirt_uri_uses_remote_transport,
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


def test_vm_disk_paths_raises_on_parse_error(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result("not xml"))
    with pytest.raises(ValueError, match="unparseable XML"):
        vm_disk_paths("qemu:///system", "alpha")


def test_vm_disk_paths_raises_on_empty_source_value(monkeypatch) -> None:
    xml = "<domain><devices><disk type='file' device='disk'><source file=''/></disk></devices></domain>"
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(xml))
    with pytest.raises(ValueError, match="missing disk source"):
        vm_disk_paths("qemu:///system", "alpha")


@pytest.mark.parametrize("uri", ["qemu+ssh://host/system", "qemu+tcp://host/system", "qemu+tls://host/system"])
def test_libvirt_uri_uses_remote_transport(uri: str) -> None:
    assert libvirt_uri_uses_remote_transport(uri)


def test_libvirt_uri_uses_remote_transport_rejects_local_uri() -> None:
    assert not libvirt_uri_uses_remote_transport("qemu:///system")
    assert not libvirt_uri_uses_remote_transport("qemu+unix:///system")


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


_BASE_XML = "<domain><name>a</name><devices><interface type='network'><source network='default'/></interface></devices></domain>"  # noqa: E501
_ENRICHED_XML = "<domain><name>a</name><currentMemory unit='KiB'>1</currentMemory><seclabel type='d'/><devices><interface type='network'><mac address='52:54:00:de:ad:be'/><source network='default'/></interface></devices></domain>"  # noqa: E501


@pytest.mark.parametrize(
    ("first_xml", "second_xml"),
    [
        # libvirtd re-renders <currentMemory>/<mac>/<seclabel>; canonicalize them.
        (_BASE_XML, _ENRICHED_XML),
        # Indentation differs across point releases.
        ("<domain><name>a</name></domain>", "<domain>\n  <name>a</name>\n</domain>"),
        # Unparseable XML must still round-trip to a stable fingerprint.
        ("not-xml", "not-xml"),
    ],
)
def test_domain_xml_fingerprint_canonicalization(monkeypatch, first_xml: str, second_xml: str) -> None:
    seen = iter([first_xml, second_xml])
    monkeypatch.setattr("libvirt_backup_system.disks.run", lambda args: _xml_result(next(seen)))
    first = domain_xml_fingerprint("qemu:///system", "alpha")
    assert first is not None
    assert first == domain_xml_fingerprint("qemu:///system", "alpha")
