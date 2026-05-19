from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path

from .logging_json import event


def stamp_is_safe(stamp: str) -> bool:
    # ``Path(stamp).name == stamp`` alone passed ".." through unchanged
    # (``Path("..").name`` is ".."), which then resolved month_dir / ".." to
    # the VM directory parent — a path traversal out of the month directory.
    # Reject the path-special names, separators, NUL/control characters, and
    # leading-dot hidden-file forms explicitly before the round-trip check.
    if not stamp or stamp in {".", ".."} or stamp.startswith("."):
        return False
    if "/" in stamp or "\\" in stamp or "\x00" in stamp:
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in stamp):
        return False
    return Path(stamp).name == stamp


def is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        event("error", "regular file check failed", path=str(path), error=str(exc))
        return False


def _open_excl_nofollow(path: Path) -> int:
    # O_NOFOLLOW + O_EXCL keeps a hostile symlink at path.parent from redirecting
    # the write outside the backup tree, matching preflight._write_probe.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def atomic_write(path: Path, content: str, vm_name: str, error_message: str, *, mtime: float | None = None) -> bool:
    parent = path.parent
    tmp_path: Path | None = None
    fd = -1
    last_error: OSError | None = None
    for _ in range(8):
        candidate = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = _open_excl_nofollow(candidate)
        except FileExistsError as exc:
            last_error = exc
            continue
        except OSError as exc:
            event("error", error_message, vm=vm_name, path=str(path), error=str(exc))
            return False
        tmp_path = candidate
        break
    if tmp_path is None or fd == -1:
        message = str(last_error) if last_error else "could not create unique tempfile"
        event("error", error_message, vm=vm_name, path=str(path), error=message)
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            # fsync the data file before rename so a crash between write and
            # rename cannot leave a zero-length file behind.
            os.fsync(handle.fileno())
        if mtime is not None:
            os.utime(tmp_path, (mtime, mtime))
        tmp_path.replace(path)
        # fsync the parent directory so the rename itself is durable. The new
        # name only survives a crash once the directory entry is committed.
        _fsync_directory(parent)
    except OSError as exc:
        if fd != -1:
            with suppress(OSError):
                os.close(fd)
        tmp_path.unlink(missing_ok=True)
        event("error", error_message, vm=vm_name, path=str(path), error=str(exc))
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        # Some filesystems (e.g. certain NFS configurations) refuse to open
        # directories for fsync. The rename is still safer than no fsync — the
        # write just falls back to "best effort durable".
        return
    try:
        with suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
