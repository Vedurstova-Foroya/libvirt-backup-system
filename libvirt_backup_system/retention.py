"""Retention as a thin wrapper around the kopia global policy.

Operator-facing env vars (``KEEP_LATEST``, ``KEEP_DAILY``, etc.) map onto
``kopia policy set --global --keep-*``. The real pruning is driven by the
kopia maintenance timer; ``prune_old_months`` keeps its old name as a
no-op for the orchestrated ``run`` path so the CLI doesn't have to
distinguish the two engines.
"""

from __future__ import annotations

from . import kopia_repo
from .config import Config


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
