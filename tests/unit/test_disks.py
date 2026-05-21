from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.disks import (
    DiskEntry,
    libvirt_uri_uses_remote_transport,
    vm_disk_paths,
    vm_disk_paths_with_targets,
)
from libvirt_backup_system.shell import CommandResult


def _fake_run_factory(xml: str):
    """Return a ``run`` replacement that yields ``xml`` on stdout."""

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args=args, returncode=0, stdout=xml, stderr="")

    return fake_run


def _patch_run(monkeypatch: pytest.MonkeyPatch, xml: str) -> None:
    monkeypatch.setattr("libvirt_backup_system.disks.run", _fake_run_factory(xml))


# ---------------------------------------------------------------------------
# libvirt_uri_uses_remote_transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri",
    [
        "qemu+ssh://user@host/system",
        "qemu+tcp://host/system",
        "qemu+tls://host/system",
    ],
)
def test_remote_transport_true_for_known_remote_schemes(uri: str) -> None:
    assert libvirt_uri_uses_remote_transport(uri) is True


@pytest.mark.parametrize(
    "uri",
    [
        "qemu:///system",
        "qemu:///session",
        "test:///default",
        "",
    ],
)
def test_remote_transport_false_for_local_schemes(uri: str) -> None:
    assert libvirt_uri_uses_remote_transport(uri) is False


# ---------------------------------------------------------------------------
# Parsing happy paths
# ---------------------------------------------------------------------------


FILE_AND_BLOCK_XML = """\
<domain type='kvm'>
  <name>alpha</name>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/alpha.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='block' device='disk'>
      <source dev='/dev/mapper/vg-data'/>
      <target dev='vdb' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/iso/seed.iso'/>
      <target dev='sda' bus='sata'/>
    </disk>
  </devices>
</domain>
"""


def test_vm_disk_paths_returns_file_and_block_paths_as_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, FILE_AND_BLOCK_XML)
    assert vm_disk_paths("qemu:///system", "alpha") == [
        "/var/lib/libvirt/images/alpha.qcow2",
        "/dev/mapper/vg-data",
    ]


def test_vm_disk_paths_with_targets_returns_disk_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, FILE_AND_BLOCK_XML)
    entries = vm_disk_paths_with_targets("qemu:///system", "alpha")
    assert entries == [
        DiskEntry(target="vda", source=Path("/var/lib/libvirt/images/alpha.qcow2")),
        DiskEntry(target="vdb", source=Path("/dev/mapper/vg-data")),
    ]


def test_vm_disk_paths_skips_cdrom_and_returns_empty_when_only_cdrom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='file' device='cdrom'>
      <source file='/iso/install.iso'/>
      <target dev='sda' bus='sata'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    assert vm_disk_paths_with_targets("qemu:///system", "alpha") == []


def test_vm_disk_paths_empty_domain_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, "<domain type='kvm'><devices/></domain>")
    assert vm_disk_paths("qemu:///system", "alpha") == []


def test_vm_disk_paths_domain_without_devices_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, "<domain type='kvm'/>")
    assert vm_disk_paths_with_targets("qemu:///system", "alpha") == []


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unparseable_xml_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, "not <xml")
    with pytest.raises(ValueError, match="unparseable XML"):
        vm_disk_paths("qemu:///system", "alpha")


def test_unsupported_disk_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='network' device='disk'>
      <source protocol='rbd' name='pool/img'/>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="unsupported disk type 'network'"):
        vm_disk_paths("qemu:///system", "alpha")


def test_missing_disk_type_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``type=""`` is treated the same as a missing/unknown type because the
    # lookup table only knows ``file`` and ``block``.
    xml = """\
<domain type='kvm'>
  <devices>
    <disk device='disk'>
      <source file='/var/lib/libvirt/images/x.qcow2'/>
      <target dev='vda'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="unsupported disk type ''"):
        vm_disk_paths("qemu:///system", "alpha")


def test_missing_source_element_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <target dev='vda' bus='virtio'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="missing disk source"):
        vm_disk_paths("qemu:///system", "alpha")


def test_missing_source_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``<source/>`` exists but lacks the file attribute the file-type lookup
    # needs.
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <source/>
      <target dev='vda'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="missing disk source"):
        vm_disk_paths("qemu:///system", "alpha")


def test_missing_target_element_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/x.qcow2'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="missing target dev"):
        vm_disk_paths("qemu:///system", "alpha")


def test_missing_target_dev_attribute_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = """\
<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/x.qcow2'/>
      <target bus='virtio'/>
    </disk>
  </devices>
</domain>
"""
    _patch_run(monkeypatch, xml)
    with pytest.raises(ValueError, match="missing target dev"):
        vm_disk_paths("qemu:///system", "alpha")


# ---------------------------------------------------------------------------
# Verify that the dumpxml call uses ``--inactive`` and routes ``uri``/``vm``
# ---------------------------------------------------------------------------


def test_dump_uses_inactive_flag_and_passes_uri_and_vm_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        captured["args"] = args
        return CommandResult(args=args, returncode=0, stdout="<domain><devices/></domain>", stderr="")

    monkeypatch.setattr("libvirt_backup_system.disks.run", fake_run)
    vm_disk_paths("qemu+ssh://host/system", "my-vm")
    assert captured["args"] == [
        "virsh",
        "-c",
        "qemu+ssh://host/system",
        "dumpxml",
        "--inactive",
        "--",
        "my-vm",
    ]
