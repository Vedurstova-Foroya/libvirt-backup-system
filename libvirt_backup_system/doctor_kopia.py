from __future__ import annotations

from . import kopia_client, kopia_repo, kopia_snapshots
from .config import Config
from .shell import CommandError


def check_local_kopia_repo(config: Config) -> list[str]:
    """Connect to the local repo and probe status."""
    if not config.get("BACKUP_PATH").strip():
        return []
    try:
        repo_path = kopia_repo.local_repo_path(config)
    except ValueError as exc:
        return [f"local kopia repo path rejected: {exc}"]
    if not kopia_repo.local_repo_exists(config):
        return [f"local kopia repo missing at {repo_path}; run install"]
    cfg = kopia_repo.ensure_local_connected(config)
    if cfg is None:
        return ["local kopia repo did not connect cleanly; run install"]
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
    try:
        peers = kopia_repo.discover_peer_repos(config)
    except kopia_repo.PeerDiscoveryError as exc:
        return [str(exc)]
    for peer in peers:
        if peer.host_id == config.get("HOST_ID"):
            continue
        if kopia_repo.ensure_peer_connected(config, peer.host_id) is None:
            failures.append(f"peer kopia repo {peer.host_id} did not connect; check password sync")
    return failures


def check_local_kopia_maintenance_probe(config: Config) -> list[str]:
    """Confirm ``kopia maintenance info`` can inspect repository maintenance state."""
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.ensure_local_connected(config)
    if cfg is None:
        return []
    try:
        kopia_client.maintenance_info(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
        )
    except CommandError as exc:
        return [f"local kopia maintenance probe failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []


def check_local_kopia_verify_probe(config: Config) -> list[str]:
    """Confirm a lightweight ``kopia snapshot verify`` succeeds."""
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.ensure_local_connected(config)
    if cfg is None:
        return []
    try:
        kopia_snapshots.snapshot_verify(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            verify_files_percent=0.0,
        )
    except CommandError as exc:
        return [f"local kopia verify probe failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []
