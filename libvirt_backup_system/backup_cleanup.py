"""Cleanup helpers for failed backup runs."""

from __future__ import annotations

from . import kopia_repo, kopia_snapshots
from .config import Config
from .logging_json import event
from .shell import CommandError
from .vms import VM


def cleanup_created_disk_snapshots(config: Config, vm: VM, run_id: str, snapshot_ids: list[str]) -> None:
    for snapshot_id in snapshot_ids:
        try:
            kopia_snapshots.snapshot_delete(
                config_file=kopia_repo.local_config_file(config),
                password_file=kopia_repo.password_file_path(config),
                cache_dir=kopia_repo.cache_dir(config),
                snapshot_id=snapshot_id,
            )
            event("info", "deleted incomplete-run disk snapshot", vm=vm.name, run_id=run_id, snapshot_id=snapshot_id)
        except CommandError as exc:
            event(
                "error",
                "disk snapshot cleanup failed",
                vm=vm.name,
                run_id=run_id,
                snapshot_id=snapshot_id,
                stderr=exc.result.stderr.strip(),
            )
