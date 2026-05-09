from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import restore_to_dir
from libvirt_backup_system.shell import CommandResult


def test_restore_to_dir(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    assert restore_to_dir("source", str(tmp_path / "restore")) == 0
    assert (tmp_path / "restore").is_dir()
    assert calls == [["virtnbdrestore", "-i", "source", "-o", "restore", "-D", str(tmp_path / "restore")]]


def test_restore_to_dir_refuses_symlink_target(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    assert restore_to_dir("source", str(link)) == 1
    assert "restore target is a symlink" in capsys.readouterr().err


def test_restore_to_dir_refuses_non_empty_dir_without_force(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    target = tmp_path / "existing"
    target.mkdir()
    (target / "preexisting").write_text("keep me\n", encoding="utf-8")

    assert restore_to_dir("source", str(target)) == 1
    assert "restore target is not empty" in capsys.readouterr().err


def test_restore_to_dir_refuses_non_directory_target(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    target = tmp_path / "file"
    target.write_text("not a dir\n", encoding="utf-8")

    assert restore_to_dir("source", str(target)) == 1
    assert "restore target is not a directory" in capsys.readouterr().err


def test_restore_to_dir_force_allows_non_empty_dir(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("ok\n", encoding="utf-8")

    assert restore_to_dir("source", str(target), force=True) == 0
    assert calls[0][:1] == ["virtnbdrestore"]


def test_restore_to_dir_accepts_existing_empty_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    target = tmp_path / "empty"
    target.mkdir()
    assert restore_to_dir("source", str(target)) == 0
