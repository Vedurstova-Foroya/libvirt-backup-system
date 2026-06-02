"""Retention as a thin wrapper around the kopia global policy.

Operator-facing env vars (``KEEP_LATEST``, ``KEEP_DAILY``, etc.) map onto
``kopia policy set --global --keep-*``. The real pruning is driven by the
kopia maintenance timer; ``prune_old_months`` keeps its old name as a
no-op for the orchestrated ``run`` path so the CLI doesn't have to
distinguish the two engines.
"""

from __future__ import annotations

from . import kopia_client, kopia_repo
from .config import Config
from .logging_json import event
from .shell import CommandError


def prune_old_months(config: Config, *, current_month: str | None = None) -> int:
    """Kopia owns chunk lifecycle, so this orchestrator step is now a no-op.

    The signature is preserved so ``cli._run_command`` keeps compiling; the
    maintenance timer (Phase 6) drives ``kopia maintenance run`` on its own
    cadence, decoupled from the backup loop.
    """
    _ = current_month
    _ = config
    return 0


def apply_policy(config: Config) -> int:
    """Refresh the kopia global policy from the env file.

    Operators run ``start`` after editing the env so the kopia policy stays
    in sync with the documented retention defaults. Idempotent; the same
    flags applied twice produce the same on-disk policy.
    """
    return kopia_repo.ensure_local_repo(config, apply_global_policy=True)


def inspect_policy(config: Config) -> dict[str, object] | None:
    """Return the current kopia global policy for the local repo."""
    config_file = kopia_repo.ensure_local_connected(config)
    if config_file is None:
        return None
    try:
        return kopia_client.policy_show_global(
            config_file=config_file,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
        )
    except CommandError as exc:
        detail = exc.result.stderr.strip() or str(exc.result.returncode)
        event("error", "kopia policy inspection failed", error=detail)
    except ValueError as exc:
        event("error", "kopia policy inspection failed", error=str(exc))
    return None
