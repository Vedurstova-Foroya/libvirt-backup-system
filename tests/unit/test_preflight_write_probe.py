from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.preflight import (
    WRITE_PROBE_NAME,
    check,
    validate_config,
)
from libvirt_backup_system.vms import VM


def _preflight_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "REQUIRE_ROOT": "true",
            "BACKUP_ESTIMATE_GB_PER_VM": "1",
            "SPACE_MARGIN_PERCENT": "20",
        }
    )
    return cfg


def _patch_valid_preflight(monkeypatch, *, available_kb: int = 2_000_000) -> None:
    monkeypatch.setattr("libvirt_backup_system.preflight.shutil.which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr("libvirt_backup_system.preflight.os.geteuid", lambda: 0)
    monkeypatch.setattr("libvirt_backup_system.preflight.list_vms", lambda config: [VM("alpha", "running")])
    monkeypatch.setattr("libvirt_backup_system.preflight._df_available_kb", lambda path: available_kb)
    monkeypatch.setattr("libvirt_backup_system.preflight._virtnbdbackup_version_failures", list)
    monkeypatch.setattr("libvirt_backup_system.preflight._validate_scratch_dir", list)


def test_check_rejects_host_root_becoming_unsafe_after_mkdir(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # The writable path re-checks subpath safety after creating host_root to
    # defend against a malicious mkdir replacing the target with a symlink.
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_valid_preflight(monkeypatch)

    checks = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.preflight.subpath_is_safe", lambda root, path: next(checks, False))

    assert check(cfg) == 1
    assert "BACKUP_PATH / HOST_ID must stay within BACKUP_PATH" in capsys.readouterr().err


def test_check_rechecks_mount_before_mkdir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    # Mount drops between the readonly check and host_root.mkdir. The writable
    # preflight must catch this before mkdir, otherwise it creates
    # BACKUP_PATH/HOST_ID on the underlying local mountpoint.
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_valid_preflight(monkeypatch)

    # Mount calls: readonly check (True), pre-mkdir recheck (False).
    mount_states = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.preflight.Path.is_mount", lambda self: next(mount_states, False))

    original_mkdir = Path.mkdir
    mkdir_calls: list[Path] = []

    def tracking_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        mkdir_calls.append(self)
        original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.preflight.Path.mkdir", tracking_mkdir)

    assert check(cfg) == 1
    err = capsys.readouterr().err
    assert "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true" in err
    host_root = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")
    assert host_root not in mkdir_calls
    assert not host_root.exists()


def test_check_rechecks_mount_after_mkdir_before_write_probe(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # Mount survives mkdir but drops before the write probe. The writable
    # preflight must re-check between mkdir and probe so it doesn't write to
    # the underlying local mountpoint.
    cfg = _preflight_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    _patch_valid_preflight(monkeypatch)

    # Mount calls: readonly (True), pre-mkdir (True), pre-probe (False).
    mount_states = iter([True, True, False])
    monkeypatch.setattr("libvirt_backup_system.preflight.Path.is_mount", lambda self: next(mount_states, False))

    def fail_open(path: object, flags: int, mode: int = 0o777) -> int:
        raise AssertionError("write probe must not run after mount loss")

    monkeypatch.setattr("libvirt_backup_system.preflight.os.open", fail_open)

    assert check(cfg) == 1
    assert "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true" in capsys.readouterr().err


def test_check_reports_write_probe_failure(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)
    _patch_valid_preflight(monkeypatch)

    def fail_open(path: object, flags: int, mode: int = 0o777) -> int:
        raise OSError("readonly")

    monkeypatch.setattr("libvirt_backup_system.preflight.os.open", fail_open)
    assert check(cfg) == 1
    assert "BACKUP_PATH must be writable" in capsys.readouterr().err


def test_check_rejects_write_probe_symlink_without_following(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _preflight_config(backup_config)
    backup_path = tmp_path / "backup"
    host_root = backup_path / "host"
    host_root.mkdir(parents=True)
    cfg.values["BACKUP_PATH"] = str(backup_path)
    _patch_valid_preflight(monkeypatch)

    target = tmp_path / "probe-target"
    target.write_text("do not touch\n", encoding="utf-8")
    probe = host_root / WRITE_PROBE_NAME
    probe.symlink_to(target)

    assert check(cfg) == 1
    assert target.read_text(encoding="utf-8") == "do not touch\n"
    assert probe.is_symlink()
    assert "BACKUP_PATH must be writable" in capsys.readouterr().err


def test_check_reports_incomplete_write_probe(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)
    _patch_valid_preflight(monkeypatch)
    monkeypatch.setattr("libvirt_backup_system.preflight.os.write", lambda fd, data: 2)

    assert check(cfg) == 1
    assert "write probe was incomplete" in capsys.readouterr().err


def test_validate_config_skips_write_probe(tmp_path: Path, monkeypatch, backup_config) -> None:
    # list-vms / verify go through validate_config and must not mutate storage.
    # Even with os.open and os.write rigged to fail, validate_config should
    # pass because the write probe is only run by ``check`` (preflight).
    cfg = _preflight_config(backup_config)
    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)

    monkeypatch.setattr(
        "libvirt_backup_system.preflight.os.open",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("validate_config must not call os.open")),
    )

    assert validate_config(cfg) == 0
    # host_root must not have been created behind the user's back either.
    assert not (backup_path / cfg.get("HOST_ID")).exists()
