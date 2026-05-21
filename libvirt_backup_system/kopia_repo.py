"""Per-host kopia repo lifecycle and peer discovery.

The system uses one filesystem-backed kopia repo per host, all sharing the
same password. Each host writes only to its own ``$BACKUP_PATH/$HOST_ID/
kopia-repo/`` tree. Cross-host listing and restore connect read-only to every
peer repo discovered under ``$BACKUP_PATH/<host>/kopia-repo/`` using the same
shared password.

Each repo connection is keyed by a separate kopia config file under
``/var/lib/libvirt-backup-system/kopia-configs/<host-id>.config``; kopia's
default single-config-file layout cannot hold multiple connections at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import kopia_client
from .config import Config, prefixed
from .logging_json import event
from .shell import CommandError

KOPIA_CONFIG_DIR = "/var/lib/libvirt-backup-system/kopia-configs"
REPO_DIR_NAME = "kopia-repo"


@dataclass(frozen=True)
class PeerRepo:
    host_id: str
    repo_path: Path
    config_file: Path


def kopia_config_root(config: Config) -> Path:
    return prefixed(KOPIA_CONFIG_DIR, config.prefix)


def password_file_path(config: Config) -> Path:
    return prefixed(config.get("KOPIA_PASSWORD_FILE"), config.prefix)


def cache_dir(config: Config) -> Path:
    return prefixed(config.get("KOPIA_CACHE_DIR"), config.prefix)


def local_repo_path(config: Config) -> Path:
    """Return the path to *this* host's repo (where backups land).

    BACKUP_PATH is treated as a concrete operator-supplied path (same as
    ``paths.backup_root``) — we do NOT route it through ``prefixed`` so the
    configured location lands where the operator put it. KOPIA_REPO_PATH
    overrides the convention but follows the same rule.
    """
    raw = config.get("KOPIA_REPO_PATH").strip()
    if raw:
        return Path(raw)
    return config.path_value("BACKUP_PATH") / config.get("HOST_ID") / REPO_DIR_NAME


def local_config_file(config: Config) -> Path:
    root = kopia_config_root(config)
    return root / f"{config.get('HOST_ID')}.config"


def peer_config_file(config: Config, host_id: str) -> Path:
    return kopia_config_root(config) / f"{host_id}.config"


def _ensure_config_dir(config: Config) -> Path:
    root = kopia_config_root(config)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def local_repo_exists(config: Config) -> bool:
    repo_path = local_repo_path(config)
    return (repo_path / "kopia.repository.f").is_file()


def ensure_local_repo(config: Config, *, apply_global_policy: bool = True) -> int:
    """Create or connect this host's repo and return 0 on success.

    Idempotent: if the repo already exists, we (re)connect the local config
    file; if not, we create it. The shared password file MUST already exist
    (see ``kopia_password.write_password_file``).
    """
    _ensure_config_dir(config)
    password = password_file_path(config)
    if not password.is_file():
        event("error", "kopia password file missing", path=str(password))
        return 1
    repo_path = local_repo_path(config)
    config_file = local_config_file(config)
    cache = cache_dir(config)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        if local_repo_exists(config):
            event("info", "connecting to existing kopia repo", path=str(repo_path))
            kopia_client.repository_connect_filesystem(
                config_file=config_file,
                repo_path=repo_path,
                password_file=password,
                cache_dir=cache,
            )
        else:
            event("info", "creating new kopia repo", path=str(repo_path))
            kopia_client.repository_create_filesystem(
                config_file=config_file,
                repo_path=repo_path,
                password_file=password,
                cache_dir=cache,
            )
    except CommandError as exc:
        event(
            "error",
            "kopia repo setup failed",
            path=str(repo_path),
            returncode=exc.result.returncode,
            stderr=exc.result.stderr.strip(),
        )
        return 1
    if apply_global_policy and _apply_global_policy(config) != 0:
        return 1
    try:
        kopia_client.maintenance_set_owner(
            config_file=config_file,
            password_file=password,
            owner=f"{config.get('HOST_ID')}@{config.get('HOST_ID')}",
            cache_dir=cache,
        )
    except CommandError as exc:
        # set-owner is a best-effort claim — kopia uses heuristics from the
        # connecting client otherwise. Log and continue rather than fail the
        # install on a non-fatal warning.
        event(
            "warning",
            "kopia maintenance owner could not be set",
            returncode=exc.result.returncode,
            stderr=exc.result.stderr.strip(),
        )
    return 0


def _int_or_none(value: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _apply_global_policy(config: Config) -> int:
    password = password_file_path(config)
    cache = cache_dir(config)
    config_file = local_config_file(config)
    try:
        kopia_client.policy_set_global(
            config_file=config_file,
            password_file=password,
            cache_dir=cache,
            keep_latest=_int_or_none(config.get("KEEP_LATEST")),
            keep_hourly=_int_or_none(config.get("KEEP_HOURLY")),
            keep_daily=_int_or_none(config.get("KEEP_DAILY")),
            keep_weekly=_int_or_none(config.get("KEEP_WEEKLY")),
            keep_monthly=_int_or_none(config.get("KEEP_MONTHLY")),
            keep_annual=_int_or_none(config.get("KEEP_ANNUAL")),
            compression=config.get("KOPIA_COMPRESSION") or None,
            splitter=config.get("KOPIA_SPLITTER") or None,
        )
    except CommandError as exc:
        event("error", "kopia policy set failed", stderr=exc.result.stderr.strip())
        return 1
    return 0


def ensure_peer_connected(config: Config, host_id: str) -> Path | None:
    """Connect (read-only) to a peer host's repo and return its config file.

    Returns ``None`` on connect failure; callers log the failure with context
    they already have.
    """
    _ensure_config_dir(config)
    backup_path = config.path_value("BACKUP_PATH")
    peer_repo = backup_path / host_id / REPO_DIR_NAME
    if not (peer_repo / "kopia.repository.f").is_file():
        return None
    config_file = peer_config_file(config, host_id)
    cache = cache_dir(config)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        kopia_client.repository_connect_filesystem(
            config_file=config_file,
            repo_path=peer_repo,
            password_file=password_file_path(config),
            cache_dir=cache,
            read_only=True,
        )
    except CommandError as exc:
        event(
            "error",
            "kopia peer repo connect failed",
            host_id=host_id,
            stderr=exc.result.stderr.strip(),
        )
        return None
    return config_file


def discover_peer_repos(config: Config) -> list[PeerRepo]:
    """List every host repo under ``BACKUP_PATH/*/kopia-repo/``.

    Each entry connects lazily — the returned ``config_file`` may not exist
    until the caller invokes ``ensure_peer_connected``.
    """
    backup_path = config.path_value("BACKUP_PATH")
    if not backup_path.is_dir():
        return []
    peers: list[PeerRepo] = []
    try:
        entries = sorted(backup_path.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        event("error", "kopia peer discovery failed", backup_path=str(backup_path), error=str(exc))
        return []
    for host_dir in entries:
        if not host_dir.is_dir():
            continue
        repo_path = host_dir / REPO_DIR_NAME
        if not (repo_path / "kopia.repository.f").is_file():
            continue
        peers.append(
            PeerRepo(
                host_id=host_dir.name,
                repo_path=repo_path,
                config_file=peer_config_file(config, host_dir.name),
            )
        )
    return peers


def iter_connected_peers(config: Config) -> list[PeerRepo]:
    """Discover peer repos and connect them; skip the ones we cannot reach."""
    reachable: list[PeerRepo] = []
    for peer in discover_peer_repos(config):
        path = ensure_peer_connected(config, peer.host_id)
        if path is None:
            continue
        reachable.append(peer)
    return reachable
