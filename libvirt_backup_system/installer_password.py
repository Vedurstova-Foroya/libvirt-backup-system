"""Install-time + change-password orchestration for the kopia password file.

The actual mode-600/atomic-write + flag-resolution helpers live in
``kopia_password``. This module wires those into the install entry point
(idempotent password write or hard fail on mismatch) and into the
``change-password`` CLI subcommand (rotate kopia's master key then
overwrite the file). Splitting it out keeps ``installer.py`` under the
project's 300-LOC ceiling.
"""

from __future__ import annotations

from . import kopia_client, kopia_password, kopia_repo
from .config import Config
from .logging_json import event
from .shell import CommandError


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
    if existing_path.exists() or existing_path.is_symlink():
        security_failure = kopia_password.password_file_security_failure(existing_path)
        if security_failure is not None:
            event(
                "error",
                "kopia password file security failure",
                password_file=str(existing_path),
                error=security_failure,
            )
            return 1
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
    repo_exists = kopia_repo.local_repo_exists(cfg)
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
    if repo_exists:
        if not _supplied_password_connects_existing_repo(cfg, resolved):
            return 1
        return _replace_password_file(cfg, resolved, message="kopia password file restored")
    return _replace_password_file(cfg, resolved, message="kopia password file installed")


def _replace_password_file(cfg: Config, value: str, *, message: str) -> int:
    try:
        kopia_password.write_password_file(cfg, value)
    except OSError as exc:
        event("error", "kopia password file write failed", error=str(exc))
        return 1
    existing_path = kopia_repo.password_file_path(cfg)
    event("info", message, path=str(existing_path))
    return 0


def _supplied_password_connects_existing_repo(cfg: Config, value: str) -> bool:
    try:
        repo_path = kopia_repo.local_repo_path(cfg)
    except ValueError as exc:
        event("error", "kopia repo path rejected", error=str(exc))
        return False
    config_file = kopia_repo.local_config_file(cfg)
    config_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = kopia_repo.cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        with kopia_password.temporary_password_file(cfg, value) as password_file:
            kopia_client.repository_connect_filesystem(
                config_file=config_file,
                repo_path=repo_path,
                password_file=password_file,
                cache_dir=cache,
            )
    except CommandError as exc:
        event(
            "error",
            "supplied kopia password did not connect to existing repo",
            path=str(repo_path),
            stderr=exc.result.stderr.strip(),
        )
        return False
    except OSError as exc:
        event("error", "temporary kopia password validation failed", error=str(exc))
        return False
    event("info", "supplied kopia password validated against existing repo", path=str(repo_path))
    return True


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
