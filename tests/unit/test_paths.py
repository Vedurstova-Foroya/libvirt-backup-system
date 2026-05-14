from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.paths import write_name_marker


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
