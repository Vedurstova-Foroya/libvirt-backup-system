from __future__ import annotations

import datetime as dt
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import stamp_is_safe
from .config import Config, prefixed
from .list_restore_points import STAMP_FORMAT, BackupRow, enumerate_backups
from .logging_json import event
from .paths import runtime_backup_path_ok
from .restore_define import RESTORED_CONFIG_FILE, define_restored_domain
from .restore_metadata import BackupDomainConfig, read_backup_domain_config
from .run_records import chain_is_poisoned
from .shell import CommandError, run, run_streamed
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid

RESTORE_STAGING_DIR = Path("/var/lib/libvirt-backup-system/restore")
_TARGET_EXISTS_RE = re.compile(r"Target file already exists: \[(?P<path>[^\]]+)\]")


def _timestamp_is_well_formed(value: str) -> bool:
    """Accept only the compact ``YYYYMMDDTHHMMSS`` form that list-restore-points emits.

    The operator copies the timestamp verbatim from the listing, so the parser
    matches that exact shape. Free-form ISO strings would tempt us to silently
    pick the "closest" run, which is exactly the ``--at`` behavior we removed.
    """
    if not stamp_is_safe(value):
        return False
    try:
        dt.datetime.strptime(value, STAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    return True


def _find_match(config: Config, vm_uuid: str, timestamp: str) -> BackupRow | None:
    for row in enumerate_backups(config, vm_uuid=vm_uuid):
        if row.timestamp == timestamp:
            return row
    return None


def _local_domain_name_for_uuid(config: Config, vm_uuid: str) -> str | None:
    """Return the VM name for ``vm_uuid`` if libvirt knows about it locally.

    ``virsh domname <uuid>`` exits non-zero when no domain matches, and a
    libvirt that is entirely unreachable also fails. Either case returns
    ``None`` so the caller picks the turnkey-define path rather than trying
    to overwrite a nonexistent VM.
    """
    try:
        result = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domname", "--", vm_uuid])
    except (CommandError, OSError):
        return None
    name = result.stdout.strip()
    return name or None


def _shutdown_domain(config: Config, vm_name: str) -> bool:
    """Force the domain off so its disk files can be replaced.

    ``virsh destroy`` on an already-stopped domain returns nonzero, so the
    failure is logged at info level and we continue: a stopped domain is the
    desired state. A genuine "could not shut it down" failure is detected by
    the subsequent ``domstate`` check.
    """
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "destroy", "--", vm_name])
    except CommandError as exc:
        event("info", "destroy returned nonzero (likely already off)", vm=vm_name, stderr=exc.result.stderr.strip())
    except OSError as exc:
        event("error", "virsh destroy unavailable", vm=vm_name, error=str(exc))
        return False
    try:
        state = run(["virsh", "-c", config.get("LIBVIRT_URI"), "domstate", "--", vm_name]).stdout.strip()
    except (CommandError, OSError) as exc:
        event("error", "domstate check failed before restore", vm=vm_name, error=str(exc))
        return False
    if state.lower() != "shut off":
        event("error", "VM is not shut off; refusing to overwrite", vm=vm_name, state=state)
        return False
    return True


def _undefine_domain(config: Config, vm_name: str) -> bool:
    # ``--checkpoints-metadata`` is required because virtnbdbackup leaves
    # libvirt-side checkpoint metadata; ``undefine`` without it refuses.
    try:
        run(["virsh", "-c", config.get("LIBVIRT_URI"), "undefine", "--checkpoints-metadata", "--", vm_name])
    except CommandError as exc:
        event("error", "undefine failed", vm=vm_name, stderr=exc.result.stderr.strip())
        return False
    except OSError as exc:
        event("error", "virsh undefine unavailable", vm=vm_name, error=str(exc))
        return False
    return True


def _prepare_staging(root: Path, vm_uuid: str, timestamp: str) -> Path | None:
    """Clear and recreate the per-restore staging directory under ``root``.

    A leftover staging dir from a previous interrupted restore would otherwise
    confuse ``virtnbdrestore``: its data files share names with the freshly
    extracted ones. Removing the directory first guarantees the restore sees
    only the chain it just wrote. The path lives under a root-owned state dir
    so user-writable racing is not a concern, but ``subpath_is_safe`` still
    refuses any value that escapes the staging root.
    """
    staging = root / f"{vm_uuid}-{timestamp}"
    if not subpath_is_safe(root, staging):
        event("error", "restore staging path is unsafe", path=str(staging))
        return None
    with suppress(FileNotFoundError):
        shutil.rmtree(staging)
    try:
        staging.mkdir(parents=True, mode=0o700)
    except OSError as exc:
        event("error", "restore staging dir creation failed", path=str(staging), error=str(exc))
        return None
    return staging


@dataclass(frozen=True)
class _CheckpointDecision:
    checkpoint: str | None
    refused: bool


@dataclass(frozen=True)
class _RestoreTarget:
    staging: Path
    checkpoint: str | None
    backup_config: BackupDomainConfig
    verbose: bool


def _resolve_checkpoint(row: BackupRow) -> _CheckpointDecision:
    """Return the checkpoint or chain-end intent for a backup row.

    A poisoned chain is refused outright: the chain end may include a half-
    written checkpoint, so replaying past the last recorded run is unsafe.
    Legacy chains (no ``runs.jsonl``) restore at chain-end so the checkpoint
    is ``None`` and ``--until`` is omitted in the virtnbdrestore invocation.
    Modern chains carry their checkpoint on the row itself; ``select_checkpoint``
    is not consulted because list-restore-points already mapped the timestamp
    to the exact checkpoint and there is no ambiguity to re-resolve.
    """
    if row.checkpoint is not None:
        return _CheckpointDecision(row.checkpoint, refused=False)
    if chain_is_poisoned(row.chain_dir):
        event("error", "restore refused poisoned chain", chain_dir=str(row.chain_dir))
        return _CheckpointDecision(None, refused=True)
    return _CheckpointDecision(None, refused=False)


def _restore_target_name(
    row: BackupRow, backup_config: BackupDomainConfig, preferred_name: str | None = None
) -> str | None:
    for name in (preferred_name, row.vm_name, backup_config.name):
        if name is not None and is_safe_vm_name(name):
            return name
    return None


def _remove_existing_disk_files(disk_paths: tuple[Path, ...]) -> bool:
    for path in disk_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            event("error", "restore could not remove existing disk file", path=str(path), error=str(exc))
            return False
    return True


def _virtnbdrestore_cmd(chain_dir: Path, output_dir: Path, checkpoint: str | None, name: str | None) -> list[str]:
    cmd = ["virtnbdrestore", "-a", "restore", "-i", str(chain_dir), "-o", str(output_dir)]
    if checkpoint is not None:
        cmd.extend(["-u", checkpoint])
    if name is not None:
        cmd.extend(["--name", name])
    cmd.extend(["-c", "-C", RESTORED_CONFIG_FILE])
    return cmd


def _run_virtnbdrestore(cmd: list[str], *, verbose: bool) -> bool:
    try:
        if verbose:
            run_streamed(cmd)
        else:
            run(cmd)
    except CommandError as exc:
        stderr = exc.result.stderr.strip()
        if verbose:
            event("error", "restore failed", stderr=stderr, returncode=exc.result.returncode)
        else:
            event("error", "restore failed; rerun with --verbose for full output", returncode=exc.result.returncode)
        if match := _TARGET_EXISTS_RE.search(stderr):
            event(
                "error",
                "restore target disk already exists; move or remove it before retrying",
                path=match.group("path"),
            )
        return False
    except OSError as exc:
        event("error", "restore failed: virtnbdrestore unavailable", error=str(exc))
        return False
    return True


def _restore_overwrite(
    config: Config,
    row: BackupRow,
    target: _RestoreTarget,
    vm_name: str,
) -> int:
    if not _shutdown_domain(config, vm_name):
        return 1
    if not _undefine_domain(config, vm_name):
        return 1
    output_dir = target.backup_config.disk_output_dir or target.staging
    if output_dir != target.staging and not _remove_existing_disk_files(target.backup_config.disk_paths):
        return 1
    if target.verbose:
        event("info", "restore overwrite started", vm=vm_name, source=str(row.chain_dir), output=str(output_dir))
    name = _restore_target_name(row, target.backup_config, vm_name)
    cmd = _virtnbdrestore_cmd(row.chain_dir, output_dir, target.checkpoint, name)
    if not _run_virtnbdrestore(cmd, verbose=target.verbose):
        return 1
    if not define_restored_domain(config, output_dir / RESTORED_CONFIG_FILE, row.vm_uuid, name):
        return 1
    event("info", "restore overwrite completed", vm=vm_name, output=str(output_dir))
    return 0


def _restore_turnkey(config: Config, row: BackupRow, target: _RestoreTarget) -> int:
    """Cross-host / fresh path: restore disks, then define the adjusted domain XML."""
    output_dir = target.backup_config.disk_output_dir or target.staging
    if target.verbose:
        event("info", "restore turnkey started", vm_uuid=row.vm_uuid, source=str(row.chain_dir), output=str(output_dir))
    name = _restore_target_name(row, target.backup_config)
    cmd = _virtnbdrestore_cmd(row.chain_dir, output_dir, target.checkpoint, name)
    if not _run_virtnbdrestore(cmd, verbose=target.verbose):
        return 1
    if not define_restored_domain(config, output_dir / RESTORED_CONFIG_FILE, row.vm_uuid, name):
        return 1
    event("info", "restore turnkey completed", vm_uuid=row.vm_uuid, host_id=row.host_id, output=str(output_dir))
    return 0


def _ensure_staging_root(config: Config) -> Path | None:
    root = prefixed(RESTORE_STAGING_DIR, config.prefix)
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        event("error", "restore staging root creation failed", path=str(root), error=str(exc))
        return None
    return root


def restore(config: Config, vm_uuid: str, timestamp: str, *, verbose: bool = True) -> int:
    if not is_safe_vm_uuid(vm_uuid):
        event("error", "restore vm_uuid is not a valid UUID", vm_uuid=vm_uuid)
        return 1
    if not _timestamp_is_well_formed(timestamp):
        event("error", "restore timestamp is malformed", timestamp=timestamp)
        return 1
    if not runtime_backup_path_ok(config):
        return 1
    row = _find_match(config, vm_uuid, timestamp)
    if row is None:
        event("error", "restore found no backup matching uuid and timestamp", vm_uuid=vm_uuid, timestamp=timestamp)
        return 1
    decision = _resolve_checkpoint(row)
    if decision.refused:
        return 1
    staging_root = _ensure_staging_root(config)
    if staging_root is None:
        return 1
    staging = _prepare_staging(staging_root, vm_uuid, timestamp)
    if staging is None:
        return 1
    local_name = _local_domain_name_for_uuid(config, vm_uuid)
    same_host = row.host_id == config.get("HOST_ID")
    target = _RestoreTarget(staging, decision.checkpoint, read_backup_domain_config(row.chain_dir), verbose)
    if same_host and local_name is not None:
        return _restore_overwrite(config, row, target, local_name)
    return _restore_turnkey(config, row, target)
