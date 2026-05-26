from __future__ import annotations

from . import kopia_client, kopia_repo, kopia_snapshots
from .config import Config
from .shell import CommandError


def check_local_kopia_repo(config: Config) -> list[str]:
    """Connect to the local repo and probe status."""
    if not config.get("BACKUP_PATH").strip():
        return []
    if not kopia_repo.local_repo_exists(config):
        return [f"local kopia repo missing at {kopia_repo.local_repo_path(config)}; run install"]
    cfg = kopia_repo.local_config_file(config)
    if not cfg.is_file():
        return [f"local kopia config-file missing: {cfg}; run install"]
    try:
        kopia_client.repository_status(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
        )
    except (CommandError, ValueError) as exc:
        return [f"local kopia repo did not connect cleanly: {exc}"]
    return []


def check_peer_kopia_repos(config: Config) -> list[str]:
    """Read-only connect to every peer repo as a cross-host smoke test."""
    failures: list[str] = []
    for peer in kopia_repo.discover_peer_repos(config):
        if peer.host_id == config.get("HOST_ID"):
            continue
        if kopia_repo.ensure_peer_connected(config, peer.host_id) is None:
            failures.append(f"peer kopia repo {peer.host_id} did not connect; check password sync")
    return failures


def check_local_kopia_maintenance_dry_run(config: Config) -> list[str]:
    """Confirm ``kopia maintenance run --dry-run`` would succeed."""
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.local_config_file(config)
    if not kopia_repo.local_repo_exists(config) or not cfg.is_file():
        return []
    try:
        kopia_client.maintenance_run(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            dry_run=True,
        )
    except CommandError as exc:
        return [f"local kopia maintenance dry-run failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []


def check_local_kopia_verify_dry_run(config: Config) -> list[str]:
    """Confirm ``kopia snapshot verify --dry-run`` would succeed."""
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.local_config_file(config)
    if not kopia_repo.local_repo_exists(config) or not cfg.is_file():
        return []
    try:
        kopia_snapshots.snapshot_verify(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            dry_run=True,
        )
    except CommandError as exc:
        return [f"local kopia verify dry-run failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []
