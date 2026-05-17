from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path

from .config import Config
from .logging_json import event
from .storage import subpath_is_safe

# SHA-256 hexdigest length. The fingerprint we write is ``hashlib.sha256(...
# ).hexdigest()`` so anything that doesn't match the 64-char-lowercase-hex
# shape is corruption (truncated write, hand-edit, partial restore from a
# different format) that must force a safe recopy rather than silently match.
_FINGERPRINT_HEX_LEN = 64
_FINGERPRINT_HEX_CHARS = frozenset("0123456789abcdef")


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


def _backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def marker_is_regular(marker: Path) -> bool:
    try:
        return stat.S_ISREG(marker.lstat().st_mode)
    except FileNotFoundError:
        return False
    except OSError as exc:
        event("error", "inactive marker check failed", marker=str(marker), error=str(exc))
        return False


def remove_marker(marker: Path, vm_name: str) -> None:
    try:
        marker.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        event("error", "inactive marker removal failed", vm=vm_name, marker=str(marker), error=str(exc))


def write_marker(marker: Path, stamp: str, fingerprint: str, vm_name: str, *, mtime: float | None = None) -> bool:
    return atomic_write(marker, f"{stamp}\n{fingerprint}\n", vm_name, "inactive marker write failed", mtime=mtime)


def _read_marker_lines(marker: Path) -> list[str] | None:
    if not marker_is_regular(marker):
        return None
    try:
        return marker.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        event("error", "inactive marker read failed", marker=str(marker), error=str(exc))
        return None


def _is_well_formed_fingerprint(value: str) -> bool:
    return len(value) == _FINGERPRINT_HEX_LEN and all(c in _FINGERPRINT_HEX_CHARS for c in value)


def read_fingerprint(marker: Path) -> str | None:
    lines = _read_marker_lines(marker)
    if lines is None or len(lines) < 2:
        return None
    value = lines[1].strip()
    if not value:
        return None
    if not _is_well_formed_fingerprint(value):
        # A truncated write or a hand-edited marker can leave bytes that are
        # not a valid 64-char hex SHA-256. Returning the malformed string
        # would happen to mismatch a freshly-computed fingerprint and force
        # a recopy by accident; returning None makes the intent explicit so
        # the caller's "no fingerprint => stale" branch fires deterministically.
        return None
    return value


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
            # rename cannot leave a zero-length marker behind. Without it a
            # power loss could lose the freshness stamp and force a redundant
            # monthly copy on the next run.
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
        # marker just falls back to "best effort durable".
        return
    try:
        with suppress(OSError):
            os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def marked_backup_dir(config: Config, month_dir: Path, marker: Path, vm_name: str) -> Path | None:
    lines = _read_marker_lines(marker)
    if lines is None:
        return None
    if len(lines) != 2 or not lines[0] or not lines[1]:
        event("info", "inactive marker is malformed, recopying", vm=vm_name, marker=str(marker))
        return None
    stamp = lines[0]
    if not stamp_is_safe(stamp):
        event("error", "inactive marker stamp is unsafe, recopying", vm=vm_name, marker=str(marker), stamp=stamp)
        return None
    backup_dir = month_dir / stamp
    if not _backup_subpath_is_safe(config, backup_dir):
        event("error", "inactive marker backup path is unsafe, recopying", vm=vm_name, path=str(backup_dir))
        return None
    try:
        if backup_dir.is_dir():
            return backup_dir
    except OSError as exc:
        event(
            "error",
            "inactive marker backup directory check failed",
            vm=vm_name,
            path=str(backup_dir),
            error=str(exc),
        )
        return None
    event("info", "inactive marker backup directory is missing, recopying", vm=vm_name, path=str(backup_dir))
    return None
