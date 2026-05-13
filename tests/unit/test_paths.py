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


def test_write_name_marker_refuses_to_overwrite(tmp_path: Path, capsys) -> None:
    # exist_ok=False keeps the per-run marker from being silently rewritten;
    # an existing marker means the destination was reused or operator-edited.
    (tmp_path / "vm-0.name").touch()
    write_name_marker(tmp_path, "vm-0")
    assert "vm-name marker not written" in capsys.readouterr().err
