"""Shared-password resolution + rotation for the kopia engine.

The same shared password lives on every host that participates in the
backup tree, written atomically to ``$KOPIA_PASSWORD_FILE`` (mode 600,
root-owned). Operators supply it on install via one of three flag forms
so the secret can either be passed inline or fed in via stdin (the path
config-management uses).
"""

from __future__ import annotations

import os
import secrets
import stat
import sys
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .logging_json import event
from .shell import CommandError


@dataclass(frozen=True)
class PasswordSpec:
    """One of the three install-time flag forms."""

    literal: str | None = None
    file: str | None = None
    env_var: str | None = None
    acknowledge_loss: bool = False
    acknowledge_argv_exposure: bool = False


def resolve_password(spec: PasswordSpec, *, stdin: Iterable[str] = sys.stdin) -> str | None:
    """Return the resolved password or ``None`` when no flag was supplied.

    Multiple flags are not validated here; callers should reject more than
    one via argparse mutually-exclusive groups.
    """
    if spec.literal is not None:
        return _validate_value(spec.literal)
    if spec.file is not None:
        return _read_file_or_stdin(spec.file, stdin)
    if spec.env_var is not None:
        return _read_env(spec.env_var)
    return None


def _validate_value(value: str) -> str:
    if not value:
        raise ValueError("kopia password must not be empty")
    if "\n" in value:
        raise ValueError("kopia password must not contain newline characters")
    return value


def _read_file_or_stdin(path: str, stdin: Iterable[str]) -> str:
    if path == "-":
        # Newline-terminated single line from stdin (config-management pipe).
        raw = "".join(stdin)
        value = raw.rstrip("\n")
        return _validate_value(value)
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8").rstrip("\n")
    return _validate_value(text)


def _read_env(var: str) -> str:
    value = os.environ.get(var)
    if value is None:
        raise KeyError(f"environment variable {var!r} is not set")
    return _validate_value(value)


def read_password_file(config: Config) -> str:
    """Read and validate the on-disk password file.

    Raises ``CommandError`` on mode/owner mismatch so the caller can
    surface a single uniform "fix the password file" message.
    """
    from . import kopia_repo

    path = kopia_repo.password_file_path(config)
    return read_secure_password_file(path)


def password_file_security_failure(path: Path, *, label: str = "kopia password file") -> str | None:
    """Return a human-readable security failure for ``path``, or ``None``."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return f"{label} missing: {path}; run ``libvirt-backup-system install`` with --kopia-password"
    except OSError as exc:
        return f"{label} stat failed: {path}: {exc}"
    if not stat.S_ISREG(info.st_mode):
        return f"{label} is not a regular file: {path}"
    mode = info.st_mode & 0o777
    if mode != 0o600:
        return f"{label} must be mode 600 (is {oct(mode)}): {path}"
    uid = getattr(info, "st_uid", None)
    if uid is not None and uid != 0:
        return f"{label} must be owned by root (is uid {uid}): {path}"
    return None


def read_secure_password_file(path: Path) -> str:
    """Read ``path`` only after enforcing the shared password-file contract."""
    missing = False
    try:
        path.lstat()
    except FileNotFoundError:
        missing = True
    except OSError:
        pass
    failure = password_file_security_failure(path)
    if failure is not None:
        if missing:
            raise FileNotFoundError(failure)
        raise PermissionError(failure)
    return _validate_value(path.read_text(encoding="utf-8").rstrip("\n"))


def write_password_file(config: Config, value: str) -> None:
    """Atomically write the password file mode 600, root-owned."""
    from . import kopia_repo

    if not value:
        raise ValueError("kopia password must not be empty")
    path = kopia_repo.password_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.write(fd, b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(tmp, 0, 0)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise OSError(f"chown root:root failed for {tmp}: {exc}") from exc
    failure = password_file_security_failure(tmp, label="temporary kopia password file")
    if failure is not None:
        tmp.unlink(missing_ok=True)
        raise PermissionError(failure)
    os.replace(tmp, path)


@contextmanager
def temporary_password_file(config: Config, value: str) -> Generator[Path]:
    """Yield a short-lived secure password file for install-time validation."""
    from . import kopia_repo

    if not value:
        raise ValueError("kopia password must not be empty")
    final_path = kopia_repo.password_file_path(config)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = final_path.parent / f".{final_path.name}.verify.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.write(fd, b"\n")
        os.fsync(fd)
    finally:
        os.close(fd)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            os.chown(tmp, 0, 0)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise OSError(f"chown root:root failed for {tmp}: {exc}") from exc
    failure = password_file_security_failure(tmp, label="temporary kopia password file")
    if failure is not None:
        tmp.unlink(missing_ok=True)
        raise PermissionError(failure)
    try:
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


def existing_password_matches(config: Config, value: str) -> bool:
    try:
        existing = read_password_file(config)
    except FileNotFoundError:
        return False
    return existing == value


def change_local_password(config: Config, new_value: str) -> int:
    """Rotate the local repo password to ``new_value`` and persist it.

    Two-step: first wrap kopia's master key under the new password, then
    overwrite the on-disk file. If step 1 succeeds but step 2 fails (full
    disk, transient OSError), the local repo decrypts only with
    ``new_value`` but the file still holds the old one — we log an explicit
    recovery message naming both values rather than silently losing the
    new password mid-rotation.
    """
    from . import kopia_client, kopia_repo

    try:
        old_value = read_password_file(config)
    except (OSError, ValueError) as exc:
        event("error", "current kopia password file unreadable", error=str(exc))
        return 1
    if old_value == new_value:
        event("info", "kopia password unchanged; no rotation needed")
        return 0
    config_file = kopia_repo.ensure_local_connected(config)
    if config_file is None:
        event("error", "kopia local repo did not connect with current password")
        return 1
    cache = kopia_repo.cache_dir(config)
    try:
        kopia_client.repository_status(
            config_file=config_file, password_file=kopia_repo.password_file_path(config), cache_dir=cache
        )
    except (CommandError, ValueError) as exc:
        event("error", "kopia local repo did not connect with current password", error=str(exc))
        return 1
    try:
        kopia_client.repository_change_password(
            config_file=config_file,
            password_file=kopia_repo.password_file_path(config),
            new_password=new_value,
            cache_dir=cache,
        )
    except CommandError as exc:
        event("error", "kopia change-password failed", stderr=exc.result.stderr.strip())
        return 1
    try:
        write_password_file(config, new_value)
    except OSError as exc:
        event(
            "error",
            "kopia password file write failed AFTER rotation; recover manually",
            old_password=old_value,
            new_password=new_value,
            error=str(exc),
        )
        return 1
    event("info", "kopia password rotated", path=str(kopia_repo.password_file_path(config)))
    return 0


def password_file_is_secure(path: Path) -> bool:
    """Return True iff path exists and is mode 600 (root-owned when we're root)."""
    return password_file_security_failure(path) is None
