from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.preflight import check, host_id_drift_failures, stamp_host_id_on_first_run, validate_config
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID
from tests.unit.test_preflight import _preflight_config, patch_valid_preflight


def _state_path(cfg: Config) -> Path:
    return cfg.prefix / "var/lib/libvirt-backup-system/host-id"


def test_stamp_host_id_fills_empty_existing_state(backup_config: Config) -> None:
    cfg = _preflight_config(backup_config)
    state = _state_path(cfg)
    state.parent.mkdir(parents=True)
    state.write_text("", encoding="utf-8")

    assert stamp_host_id_on_first_run(cfg) == []
    assert state.read_text(encoding="utf-8") == "host\n"


def test_stamp_host_id_allows_matching_existing_state(backup_config: Config) -> None:
    cfg = _preflight_config(backup_config)
    state = _state_path(cfg)
    state.parent.mkdir(parents=True)
    state.write_text("host\n", encoding="utf-8")

    assert stamp_host_id_on_first_run(cfg) == []
    assert state.read_text(encoding="utf-8") == "host\n"


def test_stamp_host_id_reports_state_write_failure(backup_config: Config, monkeypatch) -> None:
    cfg = _preflight_config(backup_config)
    real_write_text = Path.write_text

    def fail_write(self: Path, *args: object, **kwargs: object) -> int:
        if self == _state_path(cfg):
            raise OSError("readonly")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_write)
    assert stamp_host_id_on_first_run(cfg) == ["HOST_ID state check failed: readonly"]


def test_host_id_drift_allows_missing_or_matching_state(backup_config: Config) -> None:
    cfg = _preflight_config(backup_config)
    assert host_id_drift_failures(cfg) == []
    state = _state_path(cfg)
    state.parent.mkdir(parents=True)
    state.write_text("host\n", encoding="utf-8")
    assert host_id_drift_failures(cfg) == []


def test_host_id_drift_reports_state_read_failure(backup_config: Config, monkeypatch) -> None:
    cfg = _preflight_config(backup_config)
    state = _state_path(cfg)
    state.parent.mkdir(parents=True)
    state.write_text("host\n", encoding="utf-8")
    real_read_text = Path.read_text

    def fail_read(self: Path, *args: object, **kwargs: object) -> str:
        if self == state:
            raise OSError("nfs vanished")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_read)
    assert host_id_drift_failures(cfg) == ["HOST_ID state check failed: nfs vanished"]


def test_check_skips_host_id_stamp_when_validation_already_failed(
    backup_config: Config,
    monkeypatch,
) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["VM_BLACKLIST"] = "../escape"
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch)

    assert check(cfg, lock_held=True) == 1
    assert not _state_path(cfg).exists()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("foo\x00bar", "HOST_ID must not contain control characters or NUL"),
        ("a/b", "HOST_ID must not contain path separators or be '.'/'..'"),
        ("..", "HOST_ID must not contain path separators or be '.'/'..'"),
        (" host", "HOST_ID must not have leading or trailing whitespace"),
        ("host ", "HOST_ID must not have leading or trailing whitespace"),
        ("\u00a0host", "HOST_ID must not have leading or trailing whitespace"),
    ],
)
def test_validate_config_rejects_unsafe_host_id(backup_config, capsys, value: str, expected: str) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["HOST_ID"] = value
    assert validate_config(cfg) == 1
    assert expected in capsys.readouterr().err


def test_validate_config_reports_empty_host_id(backup_config, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["HOST_ID"] = ""
    assert validate_config(cfg) == 1
    err = capsys.readouterr().err
    assert "HOST_ID must not be empty" in err
    assert "fatal error" not in err


def test_validate_config_rejects_unsafe_vm_blacklist_entry(backup_config, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.values["VM_BLACKLIST"] = "alpha ../escape"
    assert validate_config(cfg) == 1
    assert "VM_BLACKLIST contains unsafe VM name" in capsys.readouterr().err


def test_check_lock_held_stamps_host_id_once(backup_config, monkeypatch) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    patch_valid_preflight(monkeypatch)

    assert check(cfg, lock_held=True) == 0
    assert _state_path(cfg).read_text(encoding="utf-8") == "host\n"


def test_check_lock_held_reports_host_id_drift(backup_config, monkeypatch, capsys) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    state = _state_path(cfg)
    state.parent.mkdir(parents=True)
    state.write_text("old-host\n", encoding="utf-8")
    patch_valid_preflight(monkeypatch)

    assert check(cfg, lock_held=True) == 1
    assert "HOST_ID drift detected" in capsys.readouterr().err


def _patch_check_preamble(monkeypatch, *, available_kb: int = 2_000_000) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running", ALPHA_UUID)])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: available_kb)
    monkeypatch.setattr("libvirt_backup_system.preflight._virtnbdbackup_version_failures", list)
    monkeypatch.setattr(
        "libvirt_backup_system.preflight.probe_qemu_socket_bind_with_lock",
        lambda config, vms, *, lock_held: [],
    )


def test_check_reports_missing_scratch_dir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_check_preamble(monkeypatch)
    monkeypatch.setattr("libvirt_backup_system.preflight.SCRATCH_DIR", tmp_path / "missing-scratch")
    assert check(cfg) == 1
    assert "must exist as a directory for virtnbdbackup scratch state" in capsys.readouterr().err


def test_check_does_not_create_host_id_when_boolean_is_invalid(monkeypatch, capsys, backup_config) -> None:
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


def test_validate_config_reports_first_mount_recheck_error(
    backup_config: Config,
    monkeypatch,
    capsys,
) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    patch_valid_preflight(monkeypatch)
    probes = iter([(True, None), (False, "stale handle")])
    monkeypatch.setattr("libvirt_backup_system.preflight._backup_path_is_mount", lambda _path: next(probes))

    assert check(cfg) == 1
    assert "BACKUP_PATH mount probe failed: stale handle" in capsys.readouterr().err


def test_validate_config_reports_second_mount_recheck_error(
    backup_config: Config,
    monkeypatch,
    capsys,
) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    patch_valid_preflight(monkeypatch)
    probes = iter([(True, None), (True, None), (False, "stale handle")])
    monkeypatch.setattr("libvirt_backup_system.preflight._backup_path_is_mount", lambda _path: next(probes))

    assert check(cfg) == 1
    assert "BACKUP_PATH mount probe failed: stale handle" in capsys.readouterr().err


def test_validate_config_reports_second_mount_recheck_drop(
    backup_config: Config,
    monkeypatch,
    capsys,
) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    patch_valid_preflight(monkeypatch)
    probes = iter([(True, None), (True, None), (False, None)])
    monkeypatch.setattr("libvirt_backup_system.preflight._backup_path_is_mount", lambda _path: next(probes))

    assert check(cfg) == 1
    assert "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true" in capsys.readouterr().err


def test_check_passes_when_mount_survives_all_rechecks(backup_config: Config, monkeypatch) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    patch_valid_preflight(monkeypatch)
    probes = iter([(True, None), (True, None), (True, None)])
    monkeypatch.setattr("libvirt_backup_system.preflight._backup_path_is_mount", lambda _path: next(probes))

    assert check(cfg) == 0
