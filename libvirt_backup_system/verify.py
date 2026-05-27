"""``verify`` wrapper around ``kopia snapshot verify``.

Walks the local repo by default; opt-in cross-host verification additionally
picks specific peer repos through ``include_hosts``. VM_BLACKLIST is
intentionally ignored — verifying blacklisted-VM backups is still useful for
the operator.
"""

from __future__ import annotations

from collections.abc import Iterable

from . import kopia_repo, kopia_snapshots
from .config import Config
from .logging_json import event
from .shell import CommandError

__all__ = ["verify"]


def _verify_repo(config: Config, *, host_id: str, config_file_path: object) -> bool:
    try:
        kopia_snapshots.snapshot_verify(
            config_file=config_file_path,  # type: ignore[arg-type]
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            verify_files_percent=1.0,
        )
    except CommandError as exc:
        event("error", "kopia verify failed", host_id=host_id, stderr=exc.result.stderr.strip())
        return False
    event("info", "verify passed", host_id=host_id)
    return True


def verify(config: Config, *, include_hosts: Iterable[str] | None = None) -> int:
    local_config_file = kopia_repo.ensure_local_connected(config)
    ok = local_config_file is not None and _verify_repo(
        config, host_id=config.get("HOST_ID"), config_file_path=local_config_file
    )
    if include_hosts is None:
        return 0 if ok else 1

    for host_id in dict.fromkeys(include_hosts):
        host_failure = kopia_repo.peer_host_id_failure(host_id)
        if host_failure is not None:
            event("error", "requested peer host_id rejected", host_id=host_id, reason=host_failure)
            ok = False
            continue
        peer_config_file = kopia_repo.ensure_peer_connected(config, host_id)
        if peer_config_file is None:
            event("error", "requested peer repo unavailable", host_id=host_id)
            ok = False
            continue
        if not _verify_repo(config, host_id=host_id, config_file_path=peer_config_file):
            ok = False
    return 0 if ok else 1
