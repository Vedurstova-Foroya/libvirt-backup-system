from __future__ import annotations

from pathlib import Path

from .config import Config, is_month_dir_name
from .inactive_markers import stamp_is_safe
from .logging_json import event
from .paths import backup_root, runtime_backup_path_ok
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid, resolve_vm_uuid


def _resolve_vm_root(config: Config, root: Path, name_or_uuid: str) -> Path | None:
    if not (is_safe_vm_name(name_or_uuid) or is_safe_vm_uuid(name_or_uuid)):
        event("error", "restore target name is invalid", vm=name_or_uuid)
        return None
    candidate = root / name_or_uuid
    if candidate.is_dir():
        return candidate
    uuid = resolve_vm_uuid(config, name_or_uuid)
    if uuid is None:
        event("error", "restore target not found", vm=name_or_uuid)
        return None
    resolved = root / uuid
    if resolved.is_dir():
        return resolved
    event("error", "restore target not found", vm=name_or_uuid, uuid=uuid, path=str(resolved))
    return None


def _list_month_dirs(vm_root: Path) -> list[Path]:
    return sorted(
        (p for p in vm_root.iterdir() if p.is_dir() and is_month_dir_name(p.name)),
        key=lambda p: p.name,
    )


def _list_chain_dirs(month_dir: Path) -> list[Path]:
    # Chains and inactive copy dirs both live directly under <month>/ and use a
    # stamp-shaped name; ``stamp_is_safe`` filters the marker files (which all
    # start with ``.``) and any operator junk that might have slipped in.
    return sorted(
        (p for p in month_dir.iterdir() if p.is_dir() and stamp_is_safe(p.name)),
        key=lambda p: p.name,
    )


def _pick_month(vm_root: Path, month: str | None) -> Path | None:
    months = _list_month_dirs(vm_root)
    if not months:
        event("error", "restore found no monthly backups", vm_root=str(vm_root))
        return None
    if month is None:
        return months[-1]
    if not is_month_dir_name(month):
        event("error", "restore --month is malformed", month=month)
        return None
    chosen = vm_root / month
    if not chosen.is_dir():
        event("error", "restore --month not found", month=month, path=str(chosen))
        return None
    return chosen


def _pick_chain(month_dir: Path, chain: str | None) -> Path | None:
    chains = _list_chain_dirs(month_dir)
    if not chains:
        event("error", "restore found no backups in month", month=str(month_dir))
        return None
    if chain is None:
        return chains[-1]
    if not stamp_is_safe(chain):
        event("error", "restore --chain is unsafe", chain=chain)
        return None
    chosen = month_dir / chain
    if not chosen.is_dir():
        event("error", "restore --chain not found", chain=chain, path=str(chosen))
        return None
    return chosen


def _validate_output(output: Path) -> bool:
    if output.exists():
        try:
            entries = list(output.iterdir())
        except (NotADirectoryError, OSError) as exc:
            event("error", "restore output is not a usable directory", output=str(output), error=str(exc))
            return False
        if entries:
            event("error", "restore output is not empty", output=str(output))
            return False
        return True
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        event("error", "restore output directory creation failed", output=str(output), error=str(exc))
        return False
    return True


def restore(
    config: Config,
    vm_name_or_uuid: str,
    output: Path,
    *,
    month: str | None = None,
    chain: str | None = None,
) -> int:
    if not runtime_backup_path_ok(config):
        return 1
    backup_path = config.path_value("BACKUP_PATH")
    root = backup_root(config)
    if not subpath_is_safe(backup_path, root):
        event("error", "restore skipped because backup root is unsafe", path=str(root))
        return 1
    vm_root = _resolve_vm_root(config, root, vm_name_or_uuid)
    if vm_root is None or not subpath_is_safe(backup_path, vm_root):
        if vm_root is not None:
            event("error", "restore skipped because VM root is unsafe", path=str(vm_root))
        return 1
    month_dir = _pick_month(vm_root, month)
    if month_dir is None or not subpath_is_safe(backup_path, month_dir):
        if month_dir is not None:
            event("error", "restore skipped because month path is unsafe", path=str(month_dir))
        return 1
    chain_dir = _pick_chain(month_dir, chain)
    if chain_dir is None or not subpath_is_safe(backup_path, chain_dir):
        if chain_dir is not None:
            event("error", "restore skipped because chain path is unsafe", path=str(chain_dir))
        return 1
    if not _validate_output(output):
        return 1
    cmd = ["virtnbdrestore", "-a", "restore", "-i", str(chain_dir), "-o", str(output)]
    event("info", "restore started", source=str(chain_dir), output=str(output))
    try:
        run_streamed(cmd)
    except CommandError as exc:
        event("error", "restore failed", stderr=exc.result.stderr.strip(), returncode=exc.result.returncode)
        return 1
    event("info", "restore completed", source=str(chain_dir), output=str(output))
    return 0
