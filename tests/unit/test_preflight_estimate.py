from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.preflight import (
    _disk_virtual_size_bytes,
    _estimate_required_kb,
    _parse_major_version,
    _virtnbdbackup_version_failures,
    _vm_estimated_bytes,
    check,
    validate_config,
    validate_libvirt_uri,
)
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID

from .test_preflight import _preflight_config, patch_valid_preflight


def test_disk_virtual_size_bytes_parses_qemu_img_json(monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, '{"virtual-size": 4294967296, "format": "qcow2"}\n', "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    assert _disk_virtual_size_bytes("/x.qcow2") == 4294967296


def test_vm_estimated_bytes_falls_back_for_remote_uri_without_local_probe(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: pytest.fail("remote disk list must not be queried locally"),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: pytest.fail("qemu-img must not run on remote disk paths"),
    )
    assert _vm_estimated_bytes("qemu+ssh://host/system", VM("alpha", "running", ALPHA_UUID), 11) == 11
    assert "skipping local disk introspection for remote URI" in capsys.readouterr().err


def test_vm_estimated_bytes_uses_fallback_when_virsh_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: (_ for _ in ()).throw(CommandError(CommandResult([], 1, "", "no virsh"))),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 5) == 5
    assert "disk list failed for VM" in capsys.readouterr().err


def test_vm_estimated_bytes_uses_fallback_when_qemu_img_fails(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: ["/disk.qcow2"],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: (_ for _ in ()).throw(ValueError("bad json")),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 7) == 7
    assert "qemu-img info failed for disk" in capsys.readouterr().err


def test_vm_estimated_bytes_adds_fallback_per_failed_disk(monkeypatch, capsys) -> None:
    # A failed disk after a successful one must contribute the per-VM fallback
    # rather than being absorbed by ``max(total, fallback)``. Without this, an
    # 8-disk VM where disk 1 reports 1 TB and disks 2-8 fail would estimate at
    # 1 TB instead of 1 TB + 7*fallback and silently undersize the preflight
    # space check.
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: ["/disk1.qcow2", "/disk2.qcow2"],
    )

    def fake_disk_size(path: str) -> int:
        if path == "/disk2.qcow2":
            raise ValueError("bad json")
        return 100

    monkeypatch.setattr("libvirt_backup_system.preflight._disk_virtual_size_bytes", fake_disk_size)
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 7) == 107
    assert "qemu-img info failed for disk" in capsys.readouterr().err


def test_vm_estimated_bytes_adds_fallback_for_each_failed_disk(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: ["/disk1.qcow2", "/disk2.qcow2", "/disk3.qcow2"],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: (_ for _ in ()).throw(ValueError("bad json")),
    )

    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 11) == 33


def test_vm_estimated_bytes_sums_disks(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: ["/disk1.qcow2", "/disk2.qcow2"],
    )
    sizes = iter([1000, 2500])
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._disk_virtual_size_bytes",
        lambda path: next(sizes),
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 99) == 3500


def test_vm_estimated_bytes_empty_disk_list_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.vm_disk_paths",
        lambda uri, name: [],
    )
    assert _vm_estimated_bytes("qemu:///system", VM("alpha", "running", ALPHA_UUID), 42) == 42


def test_estimate_required_kb_handles_bad_floats(backup_config) -> None:
    cfg = backup_config
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "bad"
    assert _estimate_required_kb(cfg, [VM("alpha", "running", ALPHA_UUID)]) == 0


def test_estimate_required_kb_handles_non_finite_floats(backup_config) -> None:
    cfg = backup_config
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "nan"
    assert _estimate_required_kb(cfg, [VM("alpha", "running", ALPHA_UUID)]) == 0

    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "1"
    cfg.values["BACKUP_INCREMENTAL_MULTIPLIER"] = "inf"
    assert _estimate_required_kb(cfg, [VM("alpha", "running", ALPHA_UUID)]) == 0


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


def test_parse_major_version_handles_various_strings() -> None:
    assert _parse_major_version("virtnbdbackup 2.46") == 2
    assert _parse_major_version("3") == 3
    assert _parse_major_version("none") is None


def test_validate_config_rejects_zero_incremental_multiplier(backup_config, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_INCREMENTAL_MULTIPLIER"] = "0"
    assert validate_config(cfg) == 1
    assert "BACKUP_INCREMENTAL_MULTIPLIER must be greater than 0" in capsys.readouterr().err


def test_virtnbdbackup_version_returns_no_failures_when_missing(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: None)
    assert _virtnbdbackup_version_failures() == []


def test_virtnbdbackup_version_reports_probe_oserror(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: "/bin/virtnbdbackup")

    def fail_run(args, *, check=True, env=None, timeout=None):
        raise OSError("spawn denied")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fail_run)
    failures = _virtnbdbackup_version_failures()
    assert failures == ["virtnbdbackup version probe failed: spawn denied"]


def test_virtnbdbackup_version_reports_non_zero_return_code(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: "/bin/virtnbdbackup")
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, *, check=True, env=None, timeout=None: CommandResult(args, 2, "", "boom"),
    )
    failures = _virtnbdbackup_version_failures()
    assert failures == ["virtnbdbackup --version failed: rc=2"]


def test_virtnbdbackup_version_reports_unparseable(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: "/bin/virtnbdbackup")
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, *, check=True, env=None, timeout=None: CommandResult(args, 0, "unintelligible", ""),
    )
    failures = _virtnbdbackup_version_failures()
    assert failures
    assert "unparseable" in failures[0]


def test_virtnbdbackup_version_rejects_unsupported_major(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: "/bin/virtnbdbackup")
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, *, check=True, env=None, timeout=None: CommandResult(args, 0, "virtnbdbackup 3.0", ""),
    )
    failures = _virtnbdbackup_version_failures()
    assert failures
    assert "unsupported" in failures[0]


@pytest.mark.parametrize(
    "reported",
    ["virtnbdbackup 1.9.15", "virtnbdbackup 2.28", "virtnbdbackup 2.46"],
)
def test_virtnbdbackup_version_accepts_supported_majors(monkeypatch, reported: str) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: "/bin/virtnbdbackup")
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.run",
        lambda args, *, check=True, env=None, timeout=None: CommandResult(args, 0, "", reported),
    )
    assert _virtnbdbackup_version_failures() == []
