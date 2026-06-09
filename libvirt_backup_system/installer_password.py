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
    generated = False
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
        resolved = kopia_password.generate_password()
        generated = True
    if resolved is None:
        return 0
    repo_exists = bool(cfg.get("BACKUP_PATH").strip()) and kopia_repo.local_repo_exists(cfg)
    if has_existing and not kopia_password.existing_password_matches(cfg, resolved):
        if not repo_exists and spec.acknowledge_loss and _can_replace_unused_password_for_join(cfg, resolved):
            return _replace_password_file(cfg, resolved, message="kopia password file replaced for join")
        event(
            "error",
            "kopia password file exists with a different value; "
            "use ``libvirt-backup-system change-password`` to rotate",
            password_file=str(existing_path),
        )
        return 1
    if has_existing:
        return 0
    if not generated and not spec.acknowledge_loss:
        event(
            "error",
            "kopia password loss acknowledgement required before first install",
            flag="--acknowledge-password-loss",
            recovery="store this exact password in a secrets vault; losing it on every host makes backups unreadable",
        )
        return 1
    if not _supplied_password_connects_existing_peer_repos(cfg, resolved):
        return 1
    message = "generated kopia password file installed" if generated else "kopia password file installed"
    if repo_exists:
        if not _supplied_password_connects_existing_repo(cfg, resolved):
            return 1
        message = "generated kopia password file restored" if generated else "kopia password file restored"
    return _replace_password_file(cfg, resolved, message=message)


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


def _peer_repos_requiring_shared_token(cfg: Config) -> list[kopia_repo.PeerRepo]:
    return [
        peer
        for peer in kopia_repo.discover_peer_repos(cfg)
        if peer.host_id != cfg.get("HOST_ID") and kopia_repo.peer_host_id_failure(peer.host_id) is None
    ]


def _can_replace_unused_password_for_join(cfg: Config, value: str) -> bool:
    if not cfg.get("BACKUP_PATH").strip():
        return False
    try:
        if not _peer_repos_requiring_shared_token(cfg):
            return False
    except kopia_repo.PeerDiscoveryError as exc:
        event("error", "kopia peer discovery failed during password validation", error=str(exc))
        return False
    return _supplied_password_connects_existing_peer_repos(cfg, value)


def _supplied_password_connects_existing_peer_repos(cfg: Config, value: str) -> bool:
    if not cfg.get("BACKUP_PATH").strip():
        return True
    try:
        peers = _peer_repos_requiring_shared_token(cfg)
    except kopia_repo.PeerDiscoveryError as exc:
        event("error", "kopia peer discovery failed during password validation", error=str(exc))
        return False
    if not peers:
        return True
    cache = kopia_repo.cache_dir(cfg)
    cache.mkdir(parents=True, exist_ok=True)
    kopia_repo.kopia_config_root(cfg).mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with kopia_password.temporary_password_file(cfg, value) as password_file:
            for peer in peers:
                try:
                    kopia_client.repository_connect_filesystem(
                        config_file=peer.config_file,
                        repo_path=peer.repo_path,
                        password_file=password_file,
                        cache_dir=cache,
                        read_only=True,
                    )
                except CommandError as exc:
                    event(
                        "error",
                        "supplied kopia password did not connect to existing peer repo; "
                        "run add-node on an already joined host and use that command",
                        host_id=peer.host_id,
                        path=str(peer.repo_path),
                        stderr=exc.result.stderr.strip(),
                    )
                    return False
    except OSError as exc:
        event("error", "temporary kopia peer password validation failed", error=str(exc))
        return False
    event("info", "supplied kopia password validated against existing peer repos", peer_count=len(peers))
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
