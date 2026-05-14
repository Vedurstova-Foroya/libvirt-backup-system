from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.doctor import (
    _check_units,
    _systemctl_value,
    doctor,
)
from libvirt_backup_system.installer import install
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_units import (
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
)
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _install_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Install a real layout under ``tmp_path`` and return a loaded Config.

    The default env file ships ``BACKUP_REQUIRE_NFS_MOUNT=true`` and
    ``REQUIRE_ROOT=true`` commented in. Both would make doctor's preflight pass
    reject a plain tmp directory running as a non-root test process. We rewrite
    the env file to set both to ``false`` so doctor's check layer succeeds.
    Unit-file rendering does not depend on either key, so the on-disk units
    still match what doctor re-renders.
    """
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(backup_path))
    assert install(str(tmp_path)) == 0
    monkeypatch.delenv("BACKUP_PATH")
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        .replace("# BACKUP_REQUIRE_NFS_MOUNT=true", "BACKUP_REQUIRE_NFS_MOUNT=false")
        .replace("# REQUIRE_ROOT=true", "REQUIRE_ROOT=false"),
        encoding="utf-8",
    )
    return Config.load(prefix=str(tmp_path))


def _patch_check_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the preflight portion of doctor pass without real binaries or VMs.

    Mirrors ``patch_valid_preflight`` in test_preflight.py: stubs out the bits
    doctor inherits from ``check`` (binary discovery, virtnbdbackup version,
    scratch dir probe, libvirt VM listing, NBD socket probe, df). Tests that
    want a specific check-side failure to surface just skip the relevant stub
    or override it inline.
    """
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.list_vms",
        lambda config: [VM("alpha", "running", ALPHA_UUID)],
    )
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 2_000_000)
    monkeypatch.setattr("libvirt_backup_system.preflight._virtnbdbackup_version_failures", list)
    monkeypatch.setattr("libvirt_backup_system.preflight._validate_scratch_dir", list)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.probe_qemu_socket_bind_with_lock",
        lambda config, vms, *, lock_held: [],
    )


def _fake_systemctl(
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: str = "enabled",
    active: str = "active",
    result: str = "success",
    returncode: int = 0,
) -> None:
    monkeypatch.setattr("libvirt_backup_system.doctor.systemctl_available", lambda root: True)
    values = {
        (TIMER_UNIT_NAME, "UnitFileState"): enabled,
        (TIMER_UNIT_NAME, "ActiveState"): active,
        (RUN_UNIT_NAME, "Result"): result,
    }

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        assert args[:2] == ["systemctl", "show"]
        unit = args[2]
        prop = args[3].split("=", 1)[1]
        return CommandResult(args, returncode, values[(unit, prop)] + "\n", "")

    monkeypatch.setattr("libvirt_backup_system.doctor.run", fake_run)


def test_doctor_passes_for_healthy_install(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)

    assert doctor(cfg) == 0
    assert "doctor passed" in capsys.readouterr().out


def test_doctor_reports_missing_wrapper(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    (tmp_path / "usr/local/bin/libvirt-backup-system").unlink()

    assert doctor(cfg) == 1
    assert "wrapper script missing" in capsys.readouterr().err


def test_doctor_reports_non_executable_wrapper(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    bin_path.chmod(bin_path.stat().st_mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

    assert doctor(cfg) == 1
    assert "wrapper script not executable" in capsys.readouterr().err


def test_doctor_reports_missing_package(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    package_dst = tmp_path / "opt/libvirt-backup-system/libvirt_backup_system"
    for child in package_dst.iterdir():
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            for sub in child.rglob("*"):
                if sub.is_file() or sub.is_symlink():
                    sub.unlink()
            for sub in sorted(child.rglob("*"), reverse=True):
                sub.rmdir()
            child.rmdir()
    package_dst.rmdir()

    assert doctor(cfg) == 1
    assert "package directory missing" in capsys.readouterr().err


def test_doctor_reports_missing_config_file(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    cfg.path.unlink()

    assert doctor(cfg) == 1
    assert "config file missing" in capsys.readouterr().err


def test_doctor_skips_unit_checks_when_backup_path_empty_and_units_absent(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    cfg.values["BACKUP_PATH"] = ""
    # Simulate the clean-uninstall state (BACKUP_PATH never set, or operator
    # re-ran install with empty BACKUP_PATH so install removed the units).
    for name in ("libvirt-backup-system.service", "libvirt-backup-system-check.service", "libvirt-backup-system.timer"):
        (tmp_path / "etc/systemd/system" / name).unlink()

    assert _check_units(cfg) == []
    # doctor still fails the run because validate_env_values rejects an empty
    # BACKUP_PATH; the _check_units assertion above is what proves the unit
    # branch was skipped.
    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "BACKUP_PATH must not be empty" in err
    assert "systemd unit missing" not in err
    assert "systemd units present but BACKUP_PATH is empty" not in err


def test_doctor_reports_stale_units_when_backup_path_emptied(tmp_path: Path, monkeypatch, capsys) -> None:
    # Operator hand-edited BACKUP_PATH= back to empty without re-running
    # install. validate_config flags the empty value, but the previously
    # installed unit files stay on disk. doctor should hint that install
    # needs to be re-run to bring registration back in sync with config.
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)
    cfg.values["BACKUP_PATH"] = ""

    failures = _check_units(cfg)
    assert len(failures) == 1
    assert "systemd units present but BACKUP_PATH is empty" in failures[0]
    assert "re-run install" in failures[0]


def test_systemctl_value_strips_stdout(tmp_path: Path, monkeypatch) -> None:
    # Unit-level coverage for the rc==0 branch of _systemctl_value: trailing
    # whitespace / newline from systemctl is stripped before comparison.
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "  enabled\n", "")

    monkeypatch.setattr("libvirt_backup_system.doctor.run", fake_run)
    assert _systemctl_value(TIMER_UNIT_NAME, "UnitFileState") == "enabled"


def test_cli_doctor_dispatches_to_doctor(tmp_path: Path, monkeypatch) -> None:
    # Doctor is not dispatched through systemd (unlike check/run): it inspects
    # the install from outside the unit so it can observe the last-run Result.
    from libvirt_backup_system import cli
    from libvirt_backup_system.config import DEFAULTS

    called: dict[str, object] = {}

    def fake_doctor(config: object) -> int:
        called["config"] = config
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.doctor", fake_doctor)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load",
        lambda *args, **kwargs: Config(values=dict(DEFAULTS), path=tmp_path / "x.env", prefix=tmp_path),
    )

    assert cli.main(["doctor"]) == 0
    assert "config" in called


def test_doctor_surfaces_check_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # Doctor is a superset of ``check``: any preflight failure (here, a missing
    # required binary) must appear in doctor's output, alongside any
    # install/registration findings.
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.shutil.which",
        lambda binary: None if binary == "virtnbdbackup" else f"/usr/bin/{binary}",
    )
    _fake_systemctl(monkeypatch)

    assert doctor(cfg) == 1
    err = capsys.readouterr().err
    assert "missing binary: virtnbdbackup" in err
    assert "doctor failed" in err


def test_doctor_emits_check_metadata_on_success(tmp_path: Path, monkeypatch, capsys) -> None:
    # The pass-event surfaces ``vm_count`` and ``required_kb`` so operators see
    # the same metadata as ``check`` when doctor passes.
    cfg = _install_layout(tmp_path, monkeypatch)
    _patch_check_pass(monkeypatch)
    _fake_systemctl(monkeypatch)

    assert doctor(cfg) == 0
    out = capsys.readouterr().out
    assert '"vm_count":1' in out
    assert '"required_kb":' in out


def test_wrapper_executable_bit_is_required(tmp_path: Path) -> None:
    # Sanity check that os.access(..., X_OK) actually flips with chmod in this
    # test environment — without it the "not executable" branch is untestable.
    bin_path = tmp_path / "bin"
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o644)
    assert not os.access(bin_path, os.X_OK)
    bin_path.chmod(0o755)
    assert os.access(bin_path, os.X_OK)
