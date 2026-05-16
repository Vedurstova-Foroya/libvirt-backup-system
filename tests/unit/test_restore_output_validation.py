from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from tests.unit.conftest import ALPHA_UUID


def _seed_chain(cfg: Config) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    vm_dir.mkdir(parents=True, exist_ok=True)
    (vm_dir / "2026-01").mkdir()
    (vm_dir / "2026-01" / "20260105T120000").mkdir()
    return vm_dir


def _restore_config(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def _refuse_run(monkeypatch) -> None:
    # Output validation must fail BEFORE we ever invoke virtnbdrestore: a
    # symlink/TOCTOU bypass that nonetheless triggered the subprocess would
    # already be the bug we are guarding against, so the tests pin "must
    # not run" rather than tolerating a passthrough.
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not run")),
    )


def test_restore_rejects_existing_output_under_backup_path(monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    output = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / "restore-out"
    output.mkdir()
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is inside BACKUP_PATH" in capsys.readouterr().err


def test_restore_rejects_new_output_inside_selected_chain(monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = _seed_chain(cfg)
    output = vm_dir / "2026-01" / "20260105T120000" / "restored"
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert not output.exists()
    assert "restore output is inside BACKUP_PATH" in capsys.readouterr().err


def test_restore_output_resolution_failure_is_reported(
    tmp_path: Path, monkeypatch, backup_config: Config, capsys
) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    output = tmp_path / "out"
    monkeypatch.setattr(
        "libvirt_backup_system.restore.resolved_path_is_within",
        lambda root, path: (_ for _ in ()).throw(OSError("denied")),
    )
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output path resolution failed" in capsys.readouterr().err


def test_restore_rejects_symlink_output(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # An attacker-controlled symlink pointing at an empty directory would
    # otherwise pass the "exists + empty" guard and let virtnbdrestore write
    # through the symlink. lstat-based rejection refuses it before any
    # virtnbdrestore invocation.
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    real = tmp_path / "real-empty"
    real.mkdir()
    output = tmp_path / "via-symlink"
    output.symlink_to(real)
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is a symlink" in capsys.readouterr().err


def test_restore_rejects_dangling_symlink_output(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # A dangling symlink (target does not exist) also fails: lstat catches
    # the link itself, so this never falls through to the "absent → mkdir"
    # branch which could then race with a target swap.
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    output = tmp_path / "dangling"
    output.symlink_to(tmp_path / "does-not-exist")
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is a symlink" in capsys.readouterr().err


def test_restore_output_lstat_failure_is_reported(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # lstat() can fail with PermissionError when an intermediate dir lacks
    # search permission. Surface the error rather than crashing.
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    output = tmp_path / "out"
    real_lstat = Path.lstat

    def fail(self: Path) -> object:
        if self == output:
            raise PermissionError("denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", fail)
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output stat failed" in capsys.readouterr().err


def test_restore_output_iterdir_failure_is_reported(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # Existing-empty path passes the lstat check, but iterdir() can still
    # fail (ACL change, broken filesystem) — surface that path cleanly.
    cfg = _restore_config(backup_config)
    _seed_chain(cfg)
    output = tmp_path / "out"
    output.mkdir()
    real_iterdir = Path.iterdir

    def fail(self: Path) -> object:
        if self == output:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail)
    _refuse_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is not a usable directory" in capsys.readouterr().err
