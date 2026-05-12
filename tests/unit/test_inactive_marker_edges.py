from __future__ import annotations

import os
from pathlib import Path

import pytest

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.inactive_markers import _atomic_write, marked_backup_dir, write_marker
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM


def _backup_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
            "BACKUP_RETENTION_MONTHS": "1",
        }
    )
    return cfg


def test_marked_backup_dir_rejects_empty_backup_path(tmp_path: Path, backup_config) -> None:
    cfg = _backup_config(backup_config)
    cfg.values["BACKUP_PATH"] = ""
    month_dir = tmp_path / "host/beta/2026-05"
    marker = month_dir / ".inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")

    assert marked_backup_dir(cfg, month_dir, marker, "beta") is None


def test_backup_vm_recopies_when_inactive_marker_read_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    original_read_text = Path.read_text
    calls: list[list[str]] = []

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == marker:
            raise OSError("read denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.Path.read_text", fake_read_text)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert calls
    assert "inactive marker read failed" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["\n", "only-stamp\n", "\nfingerprint-only\n", "stamp\n\n"])
def test_backup_vm_recopies_when_inactive_marker_is_malformed(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
    content: str,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text(content, encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert calls
    assert "inactive marker is malformed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad_stamp",
    ["../oldstamp", "..", ".", ".hidden", "a\\b", "a\tb", "a\x01b"],
)
def test_backup_vm_recopies_when_inactive_marker_stamp_is_unsafe(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
    bad_stamp: str,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{bad_stamp}\nfp-stub\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert calls
    assert "inactive marker stamp is unsafe" in capsys.readouterr().err


def test_backup_vm_recopies_when_inactive_marker_backup_path_is_unsafe(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (marker.parent / "oldstamp").symlink_to(outside, target_is_directory=True)
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert calls
    assert "inactive marker backup path is unsafe" in capsys.readouterr().err


def test_backup_vm_recopies_when_inactive_marker_backup_dir_check_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    backup_dir = marker.parent / "oldstamp"
    marker.parent.mkdir(parents=True)
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    original_is_dir = Path.is_dir
    calls: list[list[str]] = []

    def fake_is_dir(self: Path) -> bool:
        if self == backup_dir:
            raise OSError("is_dir denied")
        return original_is_dir(self)

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.Path.is_dir", fake_is_dir)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "newstamp")
    assert calls
    assert "inactive marker backup directory check failed" in capsys.readouterr().err


def test_atomic_write_logs_os_error(tmp_path: Path, monkeypatch, capsys) -> None:
    marker = tmp_path / "marker"

    def fail_open(path: Path) -> int:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers._open_excl_nofollow", fail_open)
    assert not write_marker(marker, "stamp", "fp", "alpha")
    assert "inactive marker write failed" in capsys.readouterr().err


def test_atomic_write_retries_on_file_exists_and_eventually_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    marker = tmp_path / "marker"

    def always_exists(path: Path) -> int:
        raise FileExistsError("collision")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers._open_excl_nofollow", always_exists)
    assert not _atomic_write(marker, "data", "alpha", "marker write failed")
    err = capsys.readouterr().err
    assert "marker write failed" in err
    assert "collision" in err


def test_atomic_write_recovers_from_fdopen_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    marker = tmp_path / "marker"

    def fail_fdopen(fd: int, mode: str, encoding: str) -> object:
        os.close(fd)
        raise OSError("fdopen denied")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.os.fdopen", fail_fdopen)
    assert not write_marker(marker, "stamp", "fp", "alpha")
    assert not list(tmp_path.glob(".marker.*.tmp"))
    assert "inactive marker write failed" in capsys.readouterr().err


def test_atomic_write_cleans_tempfile_when_rename_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    marker = tmp_path / "marker"
    original_replace = Path.replace

    def failing_replace(self: Path, target: object) -> None:
        raise OSError("rename denied")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.Path.replace", failing_replace)
    assert not write_marker(marker, "stamp", "fp", "alpha")
    # No leftover tempfiles even though replace failed: the fd was already
    # closed by the ``with`` block (so fd == -1 in the except branch), and
    # the temp file is unlinked.
    assert not list(tmp_path.glob(".marker.*.tmp"))
    assert "inactive marker write failed" in capsys.readouterr().err
    # Touch the restored function reference to satisfy linters.
    assert original_replace is not failing_replace


def test_read_marker_logs_os_error_from_read_text(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = backup_config
    cfg.values.update({"BACKUP_COMPRESS": "true", "INACTIVE_COPY_EVERY_RUN": "false", "BACKUP_RETENTION_MONTHS": "1"})
    marker = tmp_path / "backups/host/beta/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("stamp\nfp-stub\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == marker:
            raise OSError("read denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.Path.read_text", fake_read_text)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    assert backup_vm(cfg, VM("beta", "shut off"), "2026-05", "stamp")
    assert "inactive marker read failed" in capsys.readouterr().err


def test_atomic_write_tolerates_dir_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    # _fsync_directory swallows OSError from os.open on the parent. Some
    # network filesystems refuse to open a directory for fsync; the marker
    # write itself must still succeed.
    marker = tmp_path / "marker"
    original_open = os.open

    def fake_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        if str(path) == str(tmp_path):
            raise OSError("nfs refuses directory open")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.inactive_markers.os.open", fake_open)
    assert write_marker(marker, "stamp", "fp", "alpha")
    assert marker.read_text(encoding="utf-8") == "stamp\nfp\n"


def test_read_marker_treats_symlink_marker_as_missing(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("stamp\nfp\n", encoding="utf-8")
    marker_link = tmp_path / "link"
    marker_link.symlink_to(target)
    # Symlinks fail the lstat regular-file check, so read_fingerprint returns
    # None without ever following the link.
    from libvirt_backup_system.inactive_markers import read_fingerprint

    assert read_fingerprint(marker_link) is None
