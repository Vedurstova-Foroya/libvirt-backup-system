"""Install-time + change-password orchestration for the kopia password file.

The actual mode-600/atomic-write + flag-resolution helpers live in
``kopia_password``. This module wires those into the install entry point
(idempotent password write or hard fail on mismatch) and into the
``change-password`` CLI subcommand (rotate kopia's master key then
overwrite the file). Splitting it out keeps ``installer.py`` under the
project's 300-LOC ceiling.
"""

from __future__ import annotations

from . import kopia_password, kopia_repo
from .config import Config
from .logging_json import event


def install_password(cfg: Config, spec: kopia_password.PasswordSpec) -> int:
    """Resolve + persist the kopia password file at install time.

    - Flag supplied + no existing file: write it (mode 600, root-owned).
    - Flag supplied + existing file matching: idempotent no-op.
    - Flag supplied + existing file different: hard fail; point operator at
      ``change-password`` so we never silently overwrite.
    - No flag + existing file: keep using it (re-running install to refresh
      systemd units without re-typing the password).
    - No flag + no file: hard fail with usage text.
    """
    try:
        resolved = kopia_password.resolve_password(spec)
    except (ValueError, OSError, KeyError) as exc:
        event("error", "kopia password resolution failed", error=str(exc))
        return 1
    existing_path = kopia_repo.password_file_path(cfg)
    has_existing = existing_path.is_file()
    if resolved is None and not has_existing:
        event(
            "error",
            "kopia password missing; pass --kopia-password / --kopia-password-file / "
            "--kopia-password-env on first install",
            password_file=str(existing_path),
        )
        return 1
    if resolved is None:
        return 0
    if has_existing and not kopia_password.existing_password_matches(cfg, resolved):
        event(
            "error",
            "kopia password file exists with a different value; "
            "use ``libvirt-backup-system change-password`` to rotate",
            password_file=str(existing_path),
        )
        return 1
    if has_existing:
        return 0
    if not spec.acknowledge_loss:
        event(
            "error",
            "kopia password loss acknowledgement required before first install",
            flag="--acknowledge-password-loss",
            recovery="store this exact password in a secrets vault; losing it on every host makes backups unreadable",
        )
        return 1
    try:
        kopia_password.write_password_file(cfg, resolved)
    except OSError as exc:
        event("error", "kopia password file write failed", error=str(exc))
        return 1
    event("info", "kopia password file installed", path=str(existing_path))
    return 0


def change_password(cfg: Config, spec: kopia_password.PasswordSpec) -> int:
    """Resolve the new password and call ``kopia_password.change_local_password``."""
    try:
        resolved = kopia_password.resolve_password(spec)
    except (ValueError, OSError, KeyError) as exc:
        event("error", "kopia password resolution failed", error=str(exc))
        return 1
    if resolved is None:
        event(
            "error",
            "change-password requires one of --new-kopia-password / "
            "--new-kopia-password-file / --new-kopia-password-env",
        )
        return 1
    return kopia_password.change_local_password(cfg, resolved)
