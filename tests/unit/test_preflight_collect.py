"""Tests for ``preflight.collect_check_failures`` and ``preflight.check``."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import preflight
from libvirt_backup_system.vms import VM
from tests.unit._preflight_helpers import make_config, stub_environment, write_password_file


def test_collect_check_failures_clean_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch)
    failures, vm_count, _required_kb = preflight.collect_check_failures(cfg)
    assert failures == []
    assert vm_count == 1


def test_check_returns_zero_on_clean_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch)
    assert preflight.check(cfg) == 0


def test_check_returns_one_when_failures_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch, missing_binaries=("kopia",))
    assert preflight.check(cfg) == 1


def test_collect_check_failures_rejects_remote_libvirt_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg.values["LIBVIRT_URI"] = "qemu+ssh://host/system"
    write_password_file(cfg)
    stub_environment(monkeypatch)
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert any("LIBVIRT_URI must use one of these schemes" in failure for failure in failures)


def test_collect_check_failures_locked_path_stamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path, host_id="alpha")
    write_password_file(cfg)
    stub_environment(monkeypatch)
    failures, _vm_count, _kb = preflight.collect_check_failures(cfg, lock_held=True)
    assert failures == []
    state_path = preflight._host_id_state_path(cfg)
    assert state_path.read_text(encoding="utf-8").strip() == "alpha"


def test_collect_check_failures_no_vms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch, vms=[])
    failures, vm_count, _kb = preflight.collect_check_failures(cfg)
    assert "no VMs selected" in failures
    assert vm_count == 0


def test_collect_check_failures_libvirt_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch, vms_exc=RuntimeError("virsh down"))
    failures, vm_count, _kb = preflight.collect_check_failures(cfg)
    assert any("libvirt VM discovery failed: virsh down" in failure for failure in failures)
    assert vm_count == 0


def test_collect_check_failures_includes_disk_compatibility(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch)
    monkeypatch.setattr(
        preflight.disk_compat,
        "selected_vm_disk_compatibility_failures",
        lambda _cfg, vms: [f"unsupported backup disk for {vms[0].name}: raw disk"],
    )
    failures, vm_count, _kb = preflight.collect_check_failures(cfg)
    assert vm_count == 1
    assert "unsupported backup disk for alpha: raw disk" in failures


def test_collect_check_failures_checks_space_for_running_selected_vms_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    vms = [
        VM("alpha", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        VM("beta", "shut off", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    ]
    compat_seen: list[list[str]] = []
    estimate_seen: list[list[str]] = []
    stub_environment(monkeypatch, vms=vms)

    def fake_compat(_cfg: object, checked_vms: list[VM]) -> list[str]:
        compat_seen.append([vm.name for vm in checked_vms])
        return []

    def fake_estimate(_cfg: object, estimated_vms: list[VM]) -> int:
        estimate_seen.append([vm.name for vm in estimated_vms])
        return 123

    monkeypatch.setattr(preflight.disk_compat, "selected_vm_disk_compatibility_failures", fake_compat)
    monkeypatch.setattr(preflight, "_estimate_required_kb", fake_estimate)
    failures, vm_count, required_kb = preflight.collect_check_failures(cfg)
    assert failures == []
    assert vm_count == 2
    assert required_kb == 123
    assert compat_seen == [["alpha"]]
    assert estimate_seen == [["alpha"]]


def test_collect_check_failures_insufficient_space(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch, df_kb=1, estimate_kb=10**6)
    failures, _vm_count, required_kb = preflight.collect_check_failures(cfg)
    assert required_kb == 10**6
    assert any("insufficient backup space" in failure for failure in failures)


def test_collect_check_failures_df_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch)

    def boom(_path: Path) -> int:
        raise RuntimeError("df busted")

    monkeypatch.setattr(preflight, "_df_available_kb", boom)
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert any("backup space check failed: df busted" in failure for failure in failures)


def test_collect_check_failures_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    stub_environment(monkeypatch, missing_binaries=("kopia", "qemu-nbd"))
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert "missing binary: kopia" in failures
    assert "missing binary: qemu-nbd" in failures


def test_collect_check_failures_require_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    cfg.values["REQUIRE_ROOT"] = "true"
    stub_environment(monkeypatch)
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 1000)
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert "must run as root" in failures


def test_collect_check_failures_skips_root_check_when_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    cfg.values["REQUIRE_ROOT"] = "true"
    stub_environment(monkeypatch)
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    monkeypatch.setattr(preflight, "_validate_kopia_password_file", lambda _cfg: [])
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert "must run as root" not in failures


def test_collect_check_failures_skips_space_check_when_backup_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = str(tmp_path / "missing")
    write_password_file(cfg)
    stub_environment(monkeypatch)
    failures, _, _ = preflight.collect_check_failures(cfg)
    assert any("BACKUP_PATH must exist" in failure for failure in failures)
    assert all("insufficient backup space" not in failure for failure in failures)


def test_collect_check_failures_skips_stamp_when_lock_held_but_failures_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lock_held=True`` + pre-existing failures must skip stamping and the drift check."""
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    cfg.values["BACKUP_PATH"] = str(tmp_path / "missing")
    stub_environment(monkeypatch)
    failures, _, _ = preflight.collect_check_failures(cfg, lock_held=True)
    state_path = preflight._host_id_state_path(cfg)
    assert not state_path.exists()
    assert any("BACKUP_PATH must exist" in failure for failure in failures)
