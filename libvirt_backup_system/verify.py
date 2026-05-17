from __future__ import annotations

import contextlib
import tempfile
from pathlib import Path

from .config import Config, iter_month_dirs
from .logging_json import event
from .paths import backup_root
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .vms import is_safe_vm_name, is_safe_vm_uuid, resolve_vm_uuid

__all__ = ["verify"]


def _resolve_target(config: Config, root: Path, name_or_uuid: str) -> Path | None:
    # ``is_safe_vm_name`` is permissive enough to also cover UUIDs (no path
    # separators, no control chars), so we can attempt the literal-subdir
    # match (useful for UUIDs passed directly) before falling back to virsh
    # resolution.
    if not (is_safe_vm_name(name_or_uuid) or is_safe_vm_uuid(name_or_uuid)):
        event("error", "verify target name is invalid", vm=name_or_uuid)
        return None
    candidate = root / name_or_uuid
    if candidate.is_dir():
        return candidate
    # Backups created with the UUID layout live under their UUID, not their
    # name. Resolve the current UUID for the operator-supplied name via virsh
    # so ``verify --vm <name>`` keeps working post-rename.
    uuid = resolve_vm_uuid(config, name_or_uuid)
    if uuid is None:
        return None
    resolved = root / uuid
    if resolved.is_dir():
        return resolved
    event("error", "verify target not found", vm=name_or_uuid, uuid=uuid, path=str(resolved))
    return None


def _iter_verify_targets(config: Config, root: Path, name_or_uuid: str | None) -> tuple[list[Path], bool]:
    if name_or_uuid is not None:
        target = _resolve_target(config, root, name_or_uuid)
        return ([target], True) if target is not None else ([], False)
    # Filter at the source so the loop body can assume every entry is a real
    # directory: glob also matches non-dir entries (stray files, broken
    # symlinks) the operator may have dropped under backup_root.
    return sorted(p for p in root.glob("*") if p.is_dir()), True


def _verify_backup_dir(backup_dir: Path) -> bool:
    # ``-a verify`` is the documented action selector in v2.x; ``-o`` is
    # required even though verify mode does not write to it. We hand it a
    # throwaway tempdir rather than ``backup_dir`` itself so a future upstream
    # change that introduced writes to ``-o`` could never silently mutate the
    # source backup. mode=0o700 keeps the empty staging dir unreadable to
    # other users even though it stays empty for the duration of verify.
    try:
        tmp_out = tempfile.mkdtemp(prefix="virtnbdverify-")
    except OSError as exc:
        event("error", "verify staging dir creation failed", backup=str(backup_dir), error=str(exc))
        return False
    try:
        try:
            run_streamed(["virtnbdrestore", "-a", "verify", "-i", str(backup_dir), "-o", tmp_out])
        except CommandError as exc:
            event("error", "verify failed", backup=str(backup_dir), stderr=exc.result.stderr.strip())
            return False
        except OSError as exc:
            # FileNotFoundError / PermissionError from Popen — virtnbdrestore
            # is missing on this host. Surface it as a clean operator error
            # rather than the cli's generic fatal-traceback path so a
            # recovery-host operator who skipped ``check`` gets a useful
            # message.
            event("error", "verify failed: virtnbdrestore unavailable", backup=str(backup_dir), error=str(exc))
            return False
    finally:
        # Best-effort cleanup; a future upstream that does write here would
        # leave files behind, but the next run still gets a fresh tempdir so
        # leakage is bounded.
        with contextlib.suppress(OSError):
            Path(tmp_out).rmdir()
    event("info", "verify passed", backup=str(backup_dir))
    return True


def verify(config: Config, vm_name: str | None = None) -> int:
    """Run ``virtnbdrestore -a verify`` against backups already on disk.

    Unlike ``run``, ``verify`` intentionally ignores ``VM_BLACKLIST``: a VM
    that has been blacklisted today may still have valid backups from before
    it was added, and the operator should still be able to check that those
    on-disk backups are intact. The whole-VM blacklist semantics are scoped to
    *taking* backups, not to verifying or restoring them.
    """
    root = backup_root(config)
    backup_path = config.path_value("BACKUP_PATH")
    roots, name_ok = _iter_verify_targets(config, root, vm_name)
    ok = name_ok
    verified = 0
    for vm_root in roots:
        if not subpath_is_safe(backup_path, vm_root):
            event("error", "verify skipped because path is unsafe", path=str(vm_root))
            ok = False
            continue
        for month_dir in iter_month_dirs(vm_root):
            if not subpath_is_safe(backup_path, month_dir):
                event("error", "verify skipped because month path is unsafe", path=str(month_dir))
                ok = False
                continue
            for backup_dir in sorted(path for path in month_dir.iterdir() if path.is_dir()):
                if not subpath_is_safe(backup_path, backup_dir):
                    event("error", "verify skipped because backup path is unsafe", path=str(backup_dir))
                    ok = False
                    continue
                if not _verify_backup_dir(backup_dir):
                    ok = False
                verified += 1
    # ``name_ok`` is False when ``--vm <name>`` did not resolve; ``_resolve_target``
    # already logged the specific reason ("verify target not found", etc.) so a
    # second "verify found no backups" event would just double-log the same
    # cause. Skip it when the target itself was unresolvable.
    if verified == 0 and name_ok:
        event("error", "verify found no backups", vm=vm_name or None, root=str(root))
        ok = False
    return 0 if ok else 1
