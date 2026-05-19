from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.atomic_io import (
    _open_excl_nofollow,
    atomic_write,
    is_regular_file,
    stamp_is_safe,
)


def test_stamp_is_safe_accepts_chain_id() -> None:
    assert stamp_is_safe("20260507T101112")


@pytest.mark.parametrize(
    "stamp",
    ["", ".", "..", ".hidden", "../escape", "a/b", "a\\b", "bad\nname", "bad\x00name", "ok\x7f"],
)
def test_stamp_is_safe_rejects_unsafe_inputs(stamp: str) -> None:
    assert not stamp_is_safe(stamp)


def test_is_regular_file_returns_false_for_missing_path(tmp_path: Path) -> None:
    assert not is_regular_file(tmp_path / "missing")


def test_is_regular_file_returns_false_for_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.write_text("ok", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert not is_regular_file(link)


def test_is_regular_file_returns_false_when_lstat_raises(tmp_path: Path, monkeypatch, capsys) -> None:
    target = tmp_path / "real"
    target.write_text("ok", encoding="utf-8")

    def fail_lstat(self: Path) -> object:
        raise OSError("denied")

    monkeypatch.setattr(Path, "lstat", fail_lstat)
    assert not is_regular_file(target)
    assert "regular file check failed" in capsys.readouterr().err


def test_atomic_write_writes_content_and_optional_mtime(tmp_path: Path) -> None:
    target = tmp_path / "out"
    assert atomic_write(target, "payload\n", "alpha", "write failed", mtime=1234567.0)
    assert target.read_text(encoding="utf-8") == "payload\n"
    assert target.stat().st_mtime == 1234567.0


def test_atomic_write_recovers_from_tempfile_collision(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "out"
    real_open = _open_excl_nofollow
    calls = {"n": 0}

    def flaky_open(path: Path) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileExistsError("tempfile collided")
        return real_open(path)

    monkeypatch.setattr("libvirt_backup_system.atomic_io._open_excl_nofollow", flaky_open)
    assert atomic_write(target, "ok\n", "alpha", "write failed")
    assert target.read_text(encoding="utf-8") == "ok\n"


def test_atomic_write_returns_false_when_open_raises_oserror(tmp_path: Path, monkeypatch, capsys) -> None:
    target = tmp_path / "out"

    def fail_open(path: Path) -> int:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.atomic_io._open_excl_nofollow", fail_open)
    assert not atomic_write(target, "ok\n", "alpha", "write failed")
    assert "write failed" in capsys.readouterr().err


def test_atomic_write_returns_false_when_unique_tempfile_cannot_be_created(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    target = tmp_path / "out"

    def always_collide(path: Path) -> int:
        raise FileExistsError("collision")

    monkeypatch.setattr("libvirt_backup_system.atomic_io._open_excl_nofollow", always_collide)
    assert not atomic_write(target, "ok\n", "alpha", "write failed")
    assert "write failed" in capsys.readouterr().err


def test_atomic_write_returns_false_when_rename_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    target = tmp_path / "out"

    def fail_replace(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("no space")

    monkeypatch.setattr(Path, "replace", fail_replace)
    assert not atomic_write(target, "ok\n", "alpha", "rename failed")
    err = capsys.readouterr().err
    assert "rename failed" in err
    assert not target.exists()


def test_atomic_write_closes_fd_when_fdopen_raises(tmp_path: Path, monkeypatch, capsys) -> None:
    # ``os.fdopen`` rarely fails on a freshly-opened descriptor, but when it
    # does (EBADF, ENOMEM, ...) the raw fd is still owned by atomic_write —
    # the OSError handler must close it explicitly to avoid an fd leak.
    target = tmp_path / "out"
    closed: list[int] = []
    real_close = __import__("os").close

    def track_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    def fail_fdopen(*args: object, **kwargs: object) -> object:
        raise OSError("fdopen failed")

    monkeypatch.setattr("libvirt_backup_system.atomic_io.os.fdopen", fail_fdopen)
    monkeypatch.setattr("libvirt_backup_system.atomic_io.os.close", track_close)
    assert not atomic_write(target, "ok\n", "alpha", "open failed")
    assert closed, "fd was leaked when fdopen raised"
    assert "open failed" in capsys.readouterr().err


def test_atomic_write_fsync_directory_swallows_oserror(tmp_path: Path, monkeypatch) -> None:
    # Some NFS configurations reject open() on a directory; the helper must
    # treat that as "best effort durable" and still return success.
    real_open = __import__("os").open
    target = tmp_path / "out"

    def fail_dir_open(path: object, flags: int, mode: int = 0) -> int:
        if path == tmp_path:
            raise OSError("no dirfd")
        return real_open(path, flags, mode)

    monkeypatch.setattr("libvirt_backup_system.atomic_io.os.open", fail_dir_open)
    assert atomic_write(target, "ok\n", "alpha", "write failed")
    assert target.read_text(encoding="utf-8") == "ok\n"
