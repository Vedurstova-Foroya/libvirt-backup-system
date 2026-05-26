from __future__ import annotations

import os
import stat

from .config import Config, prefixed


def validate_kopia_password_file(config: Config) -> list[str]:
    """Confirm the kopia password file exists and is root-owned mode 600."""
    raw = config.get("KOPIA_PASSWORD_FILE").strip()
    if not raw:
        return ["KOPIA_PASSWORD_FILE must not be empty"]
    path = prefixed(raw, config.prefix)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [f"KOPIA_PASSWORD_FILE missing: {path}; run ``libvirt-backup-system install`` with --kopia-password"]
    except OSError as exc:
        return [f"KOPIA_PASSWORD_FILE stat failed: {path}: {exc}"]
    if not stat.S_ISREG(info.st_mode):
        return [f"KOPIA_PASSWORD_FILE is not a regular file: {path}"]
    if (info.st_mode & 0o777) != 0o600:
        return [f"KOPIA_PASSWORD_FILE must be mode 600 (is {oct(info.st_mode & 0o777)}): {path}"]
    if hasattr(os, "geteuid") and os.geteuid() == 0 and info.st_uid != 0:
        return [f"KOPIA_PASSWORD_FILE must be owned by root (is uid {info.st_uid}): {path}"]
    return []
