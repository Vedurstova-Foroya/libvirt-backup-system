from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import preflight_estimate
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "b"),
            "HOST_ID": "h",
            "BACKUP_ESTIMATE_GB_PER_VM": "1",
            "BACKUP_INCREMENTAL_MULTIPLIER": "1.2",
            "SPACE_MARGIN_PERCENT": "20",
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    return cfg


def test_df_available_kb_parses_df_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out = "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/sda 100 50 50 50% /\n"
    monkeypatch.setattr(preflight_estimate, "run", lambda args, **_: CommandResult(args, 0, out, ""))
    assert preflight_estimate.df_available_kb(tmp_path) == 50


def test_df_available_kb_raises_when_no_data_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(preflight_estimate, "run", lambda args, **_: CommandResult(args, 0, "Filesystem\n", ""))
    with pytest.raises(RuntimeError, match="data row"):
        preflight_estimate.df_available_kb(tmp_path)


def test_disk_virtual_size_bytes_parses_qemu_img(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preflight_estimate,
        "run",
        lambda args, **_: CommandResult(args, 0, '{"virtual-size": 1048576}', ""),
    )
    assert preflight_estimate.disk_virtual_size_bytes("/tmp/x.qcow2") == 1048576


def test_vm_estimated_bytes_remote_uri_uses_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    out = preflight_estimate.vm_estimated_bytes(
        "qemu+ssh://host/system", VM("a", "running", "a" * 8 + "-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), 42
    )
    assert out == 42
    assert "skipping local disk introspection" in capsys.readouterr().err


def test_vm_estimated_bytes_falls_back_when_listing_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def bad_list(uri: str, name: str) -> Any:
        raise ValueError("no disks")

    monkeypatch.setattr(preflight_estimate, "vm_disk_paths", bad_list)
    out = preflight_estimate.vm_estimated_bytes(
        "qemu:///system", VM("a", "running", "a" * 8 + "-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), 99
    )
    assert out == 99
    assert "disk list failed" in capsys.readouterr().err


def test_vm_estimated_bytes_sums_virtual_disk_sizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_estimate, "vm_disk_paths", lambda uri, name: ["/d1", "/d2"])
    sizes = iter([100, 200])
    monkeypatch.setattr(preflight_estimate, "disk_virtual_size_bytes", lambda path: next(sizes))
    out = preflight_estimate.vm_estimated_bytes(
        "qemu:///system", VM("a", "running", "a" * 8 + "-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), 0
    )
    assert out == 300


def test_vm_estimated_bytes_uses_fallback_when_qemu_img_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(preflight_estimate, "vm_disk_paths", lambda uri, name: ["/d"])

    def boom(path: str) -> int:
        raise CommandError(CommandResult(["qemu-img"], 1, "", "fail"))

    monkeypatch.setattr(preflight_estimate, "disk_virtual_size_bytes", boom)
    out = preflight_estimate.vm_estimated_bytes(
        "qemu:///system", VM("a", "running", "a" * 8 + "-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), 42
    )
    assert out == 42
    assert "qemu-img info failed" in capsys.readouterr().err


def test_estimate_required_kb_handles_invalid_floats(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "not-a-float"
    assert preflight_estimate.estimate_required_kb(cfg, []) == 0


def test_estimate_required_kb_handles_nonfinite(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.values["BACKUP_INCREMENTAL_MULTIPLIER"] = str(math.inf)
    assert preflight_estimate.estimate_required_kb(cfg, []) == 0


def test_estimate_required_kb_zero_when_no_vms(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert preflight_estimate.estimate_required_kb(cfg, []) == 0


def test_estimate_required_kb_scales_with_vms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(preflight_estimate, "vm_estimated_bytes", lambda uri, vm, fb: 1024 * 1024 * 1024)
    out = preflight_estimate.estimate_required_kb(cfg, [VM("a", "running", "a" * 8 + "-aaaa-aaaa-aaaa-aaaaaaaaaaaa")])
    # 1 GiB * 1.2 * 1.20 = 1.44 GiB → 1509949.44 KiB → int() = 1509949 + margin
    assert out > 1_000_000
