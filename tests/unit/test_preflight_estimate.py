from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.preflight import (
    _disk_virtual_size_bytes,
    _estimate_required_kb,
    _vm_disk_paths,
    _vm_estimated_bytes,
    check,
    validate_libvirt_uri,
)
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM

from .test_preflight import _preflight_config, patch_valid_preflight


def test_vm_disk_paths_parses_virsh_output(monkeypatch) -> None:
    output = (
        " Target   Source\n"
        "------------------------\n"
        " file     disk     vda      /var/lib/libvirt/images/a.qcow2\n"
        " block    disk     vdb      /dev/sdb\n"
        " file     cdrom    sda      /iso/install.iso\n"
    )

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, output, "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    assert _vm_disk_paths("qemu:///system", "alpha") == ["/var/lib/libvirt/images/a.qcow2"]


def test_disk_virtual_size_bytes_parses_qemu_img_json(monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, '{"virtual-size": 4294967296, "format": "qcow2"}\n', "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    assert _disk_virtual_size_bytes("/x.qcow2") == 4294967296


def test_vm_estimated_bytes_uses_fallback_when_virsh_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._vm_disk_paths",
        lambda uri, name: (_ for _ in ()).throw(CommandError(CommandResult([], 1, "", "no virsh"))),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running"), 5) == 5
    assert "disk list failed for VM" in capsys.readouterr().err


def test_vm_estimated_bytes_uses_fallback_when_qemu_img_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._vm_disk_paths",
        lambda uri, name: ["/disk.qcow2"],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: (_ for _ in ()).throw(ValueError("bad json")),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running"), 7) == 7
    assert "qemu-img info failed for disk" in capsys.readouterr().err


def test_vm_estimated_bytes_sums_disks(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._vm_disk_paths",
        lambda uri, name: ["/disk1.qcow2", "/disk2.qcow2"],
    )
    sizes = iter([1000, 2500])
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: next(sizes),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running"), 99) == 3500


def test_vm_estimated_bytes_empty_disk_list_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._vm_disk_paths",
        lambda uri, name: [],
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running"), 42) == 42


def test_estimate_required_kb_handles_bad_floats(backup_config) -> None:
    cfg = backup_config
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "bad"
    assert _estimate_required_kb(cfg, [VM("alpha", "running")]) == 0


def test_validate_libvirt_uri_accepts_known_schemes_and_rejects_others() -> None:
    for ok in [
        "qemu:///system",
        "qemu+ssh://host/system",
        "qemu+tcp://host/system",
        "qemu+tls://host/system",
        "qemu+unix:///run/libvirt/libvirt-sock",
        "test://default",
        "test:///default",
    ]:
        assert validate_libvirt_uri(ok), ok
    for bad in ["", "http://x", "qemu", "qemu:/", "-c qemu:///system"]:
        assert not validate_libvirt_uri(bad), bad


def test_check_rejects_unknown_libvirt_uri_scheme(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["LIBVIRT_URI"] = "http://evil.example"
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "LIBVIRT_URI must use one of these schemes" in err
