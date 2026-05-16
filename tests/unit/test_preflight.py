from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.preflight import (
    _df_available_kb,
    check,
    validate_config,
)
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _preflight_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "REQUIRE_ROOT": "true",
            "BACKUP_ESTIMATE_GB_PER_VM": "1",
            "SPACE_MARGIN_PERCENT": "20",
        }
    )
    return cfg


def patch_valid_preflight(
    monkeypatch,
    *,
    available_kb: int = 2_000_000,
    selected_vms: list[VM] | None = None,
    patch_probe: bool = True,
) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.list_vms",
        lambda config: [VM("alpha", "running", ALPHA_UUID)] if selected_vms is None else selected_vms,
    )
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: available_kb)
    monkeypatch.setattr("libvirt_backup_system.preflight._virtnbdbackup_version_failures", list)
    monkeypatch.setattr("libvirt_backup_system.preflight._validate_scratch_dir", list)
    if patch_probe:
        monkeypatch.setattr(
            "libvirt_backup_system.preflight.probe_qemu_socket_bind_with_lock",
            lambda config, vms, *, lock_held: [],
        )


def test_df_helpers(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "Filesystem 1024-blocks Used Available Capacity Mounted on\nfs 9 1 7 1% /\n", "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    assert _df_available_kb(tmp_path) == 7


def test_df_helpers_bad_output(tmp_path: Path, monkeypatch) -> None:
    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "header-only\n", "")

    monkeypatch.setattr("libvirt_backup_system.preflight.run", fake_run)
    with pytest.raises(RuntimeError, match="data row"):
        _df_available_kb(tmp_path)


def test_check_passes(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 0
    assert "preflight passed" in capsys.readouterr().out
    assert validate_config(cfg) == 0


def test_check_reports_common_failures(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_PATH"] = ""
    cfg.values["BACKUP_COMPRESS"] = "maybe"
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "bad"
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: None)
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 99)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: 1)

    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "BACKUP_PATH must not be empty" in err
    assert "BACKUP_COMPRESS must be a boolean value" in err
    assert "BACKUP_ESTIMATE_GB_PER_VM must be a number" in err
    assert "missing binary" in err
    assert "must run as root" in err
    assert "no VMs selected" in err


def test_validate_config_reports_numeric_and_path_edge_cases(
    tmp_path: Path, monkeypatch, capsys, backup_config
) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["SPACE_MARGIN_PERCENT"] = "bad"
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "-1"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "missing")
    assert validate_config(cfg) == 1
    err = capsys.readouterr().err
    assert "SPACE_MARGIN_PERCENT must be an integer" in err
    assert "BACKUP_ESTIMATE_GB_PER_VM must be greater than or equal to 0" in err
    assert "BACKUP_PATH must exist" in err
    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    cfg.values.update({"SPACE_MARGIN_PERCENT": "20", "BACKUP_ESTIMATE_GB_PER_VM": "1", "BACKUP_PATH": str(backup_path)})
    monkeypatch.setattr("libvirt_backup_system.preflight.subpath_is_safe", lambda root, path: False)
    assert validate_config(cfg) == 1
    assert "BACKUP_PATH / HOST_ID must stay within BACKUP_PATH" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_validate_config_rejects_non_finite_float_values(value: str, backup_config, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = value

    assert validate_config(cfg) == 1
    assert "BACKUP_ESTIMATE_GB_PER_VM must be a finite number" in capsys.readouterr().err


def test_check_reports_low_backup_space(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch, available_kb=1)
    assert check(cfg) == 1
    assert "insufficient backup space" in capsys.readouterr().err


def test_check_handles_discovery_and_backup_space_errors(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    raise_no_libvirt = lambda config: (_ for _ in ()).throw(RuntimeError("no libvirt"))  # noqa: E731
    raise_bad_df = lambda path: (_ for _ in ()).throw(RuntimeError("bad df"))  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", raise_no_libvirt)
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", raise_bad_df)
    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "libvirt VM discovery failed" in err
    assert "backup space check failed" in err


def test_check_reports_backup_path_failures(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    backup_file = tmp_path / "backup-file"
    backup_file.write_text("not a directory", encoding="utf-8")
    cfg.values["BACKUP_PATH"] = str(backup_file)
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 1
    assert "BACKUP_PATH must be a directory" in capsys.readouterr().err

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_dir)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    monkeypatch.setattr("libvirt_backup_system.preflight.Path.is_mount", lambda self: False)
    assert check(cfg) == 1
    assert "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true" in capsys.readouterr().err


def test_check_rejects_unsafe_host_id_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["HOST_ID"] = "../outside"
    patch_valid_preflight(monkeypatch)

    assert check(cfg) == 1
    assert not (tmp_path / "outside").exists()
    assert "BACKUP_PATH / HOST_ID must stay within BACKUP_PATH" in capsys.readouterr().err


def test_check_rejects_absolute_host_id_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    outside = tmp_path / "outside"
    cfg.values["HOST_ID"] = str(outside)
    patch_valid_preflight(monkeypatch)

    assert check(cfg) == 1
    assert not outside.exists()
    assert "BACKUP_PATH / HOST_ID must stay within BACKUP_PATH" in capsys.readouterr().err


def test_negative_space_margin_percent_rejected(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["SPACE_MARGIN_PERCENT"] = "-5"
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 1
    assert "SPACE_MARGIN_PERCENT must be greater than or equal to 0" in capsys.readouterr().err


def test_validate_config_rejects_relative_backup_path(backup_config, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_PATH"] = "relative/backups"
    assert validate_config(cfg) == 1
    assert "BACKUP_PATH must be an absolute path" in capsys.readouterr().err


def test_check_rejects_symlinked_host_id_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    backup_path = cfg.path_value("BACKUP_PATH")
    backup_path.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (backup_path / "link").symlink_to(outside, target_is_directory=True)
    cfg.values["HOST_ID"] = "link/host"
    patch_valid_preflight(monkeypatch)

    assert check(cfg) == 1
    assert not (outside / "host").exists()
    assert "BACKUP_PATH / HOST_ID must stay within BACKUP_PATH" in capsys.readouterr().err


def _patch_check_preamble(monkeypatch, *, available_kb: int = 2_000_000) -> None:
    # Like patch_valid_preflight but leaves _validate_scratch_dir alone.
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running", ALPHA_UUID)])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: available_kb)
    monkeypatch.setattr("libvirt_backup_system.preflight._virtnbdbackup_version_failures", list)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.probe_qemu_socket_bind_with_lock",
        lambda config, vms, *, lock_held: [],
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("foo\x00bar", "HOST_ID must not contain control characters or NUL"),
        ("a/b", "HOST_ID must not contain path separators or be '.'/'..'"),
        ("..", "HOST_ID must not contain path separators or be '.'/'..'"),
        (" host", "HOST_ID must not have leading or trailing whitespace"),
        ("host ", "HOST_ID must not have leading or trailing whitespace"),
        ("\u00a0host", "HOST_ID must not have leading or trailing whitespace"),  # NBSP
    ],
)
def test_validate_config_rejects_unsafe_host_id(backup_config, capsys, value: str, expected: str) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["HOST_ID"] = value
    assert validate_config(cfg) == 1
    assert expected in capsys.readouterr().err


def test_validate_config_reports_empty_host_id(backup_config, capsys) -> None:
    # Config.load returns HOST_ID="" when both config and hostname are blank;
    # preflight surfaces the clean validation error (not "fatal error").
    cfg = _preflight_config(backup_config)
    cfg.values["HOST_ID"] = ""
    assert validate_config(cfg) == 1
    err = capsys.readouterr().err
    assert "HOST_ID must not be empty" in err
    assert "fatal error" not in err


def test_check_reports_missing_scratch_dir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_check_preamble(monkeypatch)
    monkeypatch.setattr("libvirt_backup_system.preflight.SCRATCH_DIR", tmp_path / "missing-scratch")
    assert check(cfg) == 1
    assert "must exist as a directory for virtnbdbackup scratch state" in capsys.readouterr().err


def test_check_does_not_create_host_id_when_boolean_is_invalid(monkeypatch, capsys, backup_config) -> None:
    # bool_value() degrades non-true strings to False, so BACKUP_REQUIRE_NFS_MOUNT=typo
    # previously skipped the mount check and created BACKUP_PATH/HOST_ID before
    # reporting the boolean failure. Preflight must refuse to write on invalid config.
    cfg = _preflight_config(backup_config)
    backup_path = cfg.path_value("BACKUP_PATH")
    backup_path.mkdir()
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "typo"
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 1
    assert "BACKUP_REQUIRE_NFS_MOUNT must be a boolean value" in capsys.readouterr().err
    assert not (backup_path / cfg.get("HOST_ID")).exists()


def test_check_reports_unwritable_scratch_dir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_check_preamble(monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("libvirt_backup_system.preflight.SCRATCH_DIR", scratch)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight._write_probe",
        lambda path: (_ for _ in ()).throw(OSError("readonly mount")),
    )
    assert check(cfg) == 1
    assert "must be writable for virtnbdbackup scratch state" in capsys.readouterr().err
