from __future__ import annotations

import os
import secrets
import stat
from contextlib import suppress
from pathlib import Path

from .config import Config
from .logging_json import event
from .storage import subpath_is_safe

LEGACY_FINGERPRINT_FILE_NAME = ".inactive-copy-fingerprint"


def _backup_subpath_is_safe(config: Config, path: Path) -> bool:
    if not config.get("BACKUP_PATH").strip():
        return False
    return subpath_is_safe(config.path_value("BACKUP_PATH"), path)


def _legacy_fingerprint_path(marker: Path) -> Path:
    return marker.parent / LEGACY_FINGERPRINT_FILE_NAME


def remove_fingerprint(marker: Path, vm_name: str) -> None:
    # Older releases stored the fingerprint in a separate sidecar file; the
    # current marker stores both stamp and fingerprint in one atomic file, so
    # this only exists to scrub leftover sidecars on the first run after upgrade.
    path = _legacy_fingerprint_path(marker)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        event("error", "inactive fingerprint removal failed", vm=vm_name, path=str(path), error=str(exc))


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


def write_marker(marker: Path, stamp: str, fingerprint: str, vm_name: str) -> bool:
    return _atomic_write(marker, f"{stamp}\n{fingerprint}\n", vm_name, "inactive marker write failed")


def _read_marker_lines(marker: Path) -> list[str] | None:
    if not marker_is_regular(marker):
        return None
    try:
        return marker.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        event("error", "inactive marker read failed", marker=str(marker), error=str(exc))
        return None


def read_fingerprint(marker: Path) -> str | None:
    lines = _read_marker_lines(marker)
    if lines is None or len(lines) < 2:
        return None
    value = lines[1].strip()
    return value or None


def _open_excl_nofollow(path: Path) -> int:
    # O_NOFOLLOW + O_EXCL keeps a hostile symlink at path.parent from redirecting
    # the write outside the backup tree, matching preflight._write_probe.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _atomic_write(path: Path, content: str, vm_name: str, error_message: str) -> bool:
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
        tmp_path.replace(path)
    except OSError as exc:
        if fd != -1:
            with suppress(OSError):
                os.close(fd)
        tmp_path.unlink(missing_ok=True)
        event("error", error_message, vm=vm_name, path=str(path), error=str(exc))
        return False
    return True


def marked_backup_dir(config: Config, month_dir: Path, marker: Path, vm_name: str) -> Path | None:
    lines = _read_marker_lines(marker)
    if lines is None:
        return None
    if len(lines) != 2 or not lines[0] or not lines[1]:
        event("info", "inactive marker is malformed, recopying", vm=vm_name, marker=str(marker))
        return None
    stamp = lines[0]
    if Path(stamp).name != stamp:
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
