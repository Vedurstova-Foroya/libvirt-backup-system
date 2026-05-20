from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.fish_completion import (
    FISH_COMPLETION_DIR,
    FISH_COMPLETION_NAME,
    fish_completion_target,
    install_fish_completion,
    remove_fish_completion,
)


def test_packaged_completion_file_exists() -> None:
    # The package data file must ship with the repo so install_fish_completion
    # has something to copy. A missing file is a packaging bug, not a runtime
    # condition the installer should be expected to recover from.
    import libvirt_backup_system

    pkg_root = Path(libvirt_backup_system.__file__).resolve().parent
    assert (pkg_root / "data" / FISH_COMPLETION_NAME).is_file()


def test_fish_completion_target_lands_under_prefix(tmp_path: Path) -> None:
    target = fish_completion_target(tmp_path)
    assert target == tmp_path / str(FISH_COMPLETION_DIR).lstrip("/") / FISH_COMPLETION_NAME


def test_install_writes_completion_into_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fish_completion(tmp_path)
    target = fish_completion_target(tmp_path)
    assert target.is_file()
    assert "complete -c libvirt-backup-system" in target.read_text(encoding="utf-8")
    assert "installed fish completion" in capsys.readouterr().out


def test_install_swallows_oserror_on_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A read-only /usr/share or a hostile filesystem must not abort install.
    def refuse_copy(src: str, dst: str) -> str:
        raise OSError("read-only filesystem")

    monkeypatch.setattr("libvirt_backup_system.fish_completion.shutil.copyfile", refuse_copy)
    install_fish_completion(tmp_path)
    # The target does not exist but the function returned cleanly.
    assert not fish_completion_target(tmp_path).exists()
    assert "fish completion install skipped" in capsys.readouterr().err


def test_install_warns_when_package_source_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # If somebody strips the data/ directory out of the wheel the installer
    # must surface a warning rather than crash with FileNotFoundError.
    bogus = tmp_path / "does-not-exist.fish"
    monkeypatch.setattr("libvirt_backup_system.fish_completion._packaged_completion_path", lambda: bogus)
    install_fish_completion(tmp_path)
    assert "fish completion source missing in package" in capsys.readouterr().err


def test_remove_returns_true_when_already_absent(tmp_path: Path) -> None:
    assert remove_fish_completion(tmp_path) is True


def test_remove_returns_true_after_removing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    install_fish_completion(tmp_path)
    capsys.readouterr()
    assert fish_completion_target(tmp_path).is_file()
    assert remove_fish_completion(tmp_path) is True
    assert not fish_completion_target(tmp_path).exists()
    assert "removed fish completion" in capsys.readouterr().out


def test_remove_returns_false_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install_fish_completion(tmp_path)
    capsys.readouterr()

    def refuse_unlink(self: Path, *, missing_ok: bool = False) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr("libvirt_backup_system.fish_completion.Path.unlink", refuse_unlink)
    assert remove_fish_completion(tmp_path) is False
    assert "failed to remove fish completion" in capsys.readouterr().err
