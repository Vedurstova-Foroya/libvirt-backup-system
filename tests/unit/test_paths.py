from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.paths import runtime_backup_path_ok, write_name_marker


def test_write_name_marker_creates_empty_file(tmp_path: Path) -> None:
    write_name_marker(tmp_path, "vm-0")
    marker = tmp_path / "vm-0.name"
    assert marker.is_file()
    assert marker.read_bytes() == b""


def test_write_name_marker_logs_warning_when_touch_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    # Marker failure must not bubble up: the backup data itself is unaffected,
    # only the find-by-name convenience. Log a warning and move on.
    def refuse(self: Path, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.paths.Path.touch", refuse)
    write_name_marker(tmp_path, "vm-0")
    assert "vm-name marker not written" in capsys.readouterr().err


def test_write_name_marker_is_idempotent_when_marker_exists(tmp_path: Path, capsys) -> None:
    # Incremental backups reuse the chain's destination directory, so the
    # marker written by the full backup is already present. Re-creating it
    # must be silent rather than emitting a warning per-increment.
    (tmp_path / "vm-0.name").touch()
    write_name_marker(tmp_path, "vm-0")
    assert (tmp_path / "vm-0.name").is_file()
    assert "vm-name marker not written" not in capsys.readouterr().err


def test_read_fingerprint_rejects_malformed_hex(tmp_path: Path) -> None:
    # Validating against the 64-char hex SHA-256 shape turns a truncated /
    # hand-edited marker into a clean "no fingerprint -> force recopy" path.
    from libvirt_backup_system.inactive_markers import read_fingerprint

    marker = tmp_path / "marker"
    for malformed in ("short-hex", "   ", "a" * 63, "z" * 64):
        marker.write_text(f"stamp\n{malformed}\n", encoding="utf-8")
        assert read_fingerprint(marker) is None, malformed
    marker.write_text("stamp\n" + "a" * 64 + "\n", encoding="utf-8")
    assert read_fingerprint(marker) == "a" * 64


def test_runtime_backup_path_ok_returns_false_when_is_mount_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A stale NFS handle bubbles OSError out of Path.is_mount(); the helper
    # must catch it and report "no longer a mount point" instead of
    # crashing every caller that re-checks the mount between phases.
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backups")
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    (tmp_path / "backups").mkdir()

    def refuse(self: Path) -> bool:
        raise OSError("ESTALE")

    monkeypatch.setattr("libvirt_backup_system.paths.Path.is_mount", refuse)
    assert runtime_backup_path_ok(cfg) is False
    assert "BACKUP_PATH mount probe failed" in capsys.readouterr().err
