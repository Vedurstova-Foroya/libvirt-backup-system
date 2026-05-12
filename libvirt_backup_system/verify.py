from __future__ import annotations

from pathlib import Path

from .cleanup import backup_root
from .config import Config, iter_month_dirs
from .logging_json import event
from .shell import CommandError, run_streamed
from .storage import subpath_is_safe
from .vms import is_safe_vm_name

__all__ = ["verify"]


def _iter_verify_targets(root: Path, vm_name: str | None) -> tuple[list[Path], bool]:
    if vm_name is not None:
        if not is_safe_vm_name(vm_name):
            event("error", "verify target name is invalid", vm=vm_name)
            return [], False
        return [root / vm_name], True
    return sorted(root.glob("*")), True


def _verify_backup_dir(backup_dir: Path) -> bool:
    try:
        # ``-a verify`` is the documented action selector in v2.x; ``-o`` is
        # the required output directory (verify mode does not write to it).
        # The pre-v2.x ``-o verify`` shortcut still works but is undocumented
        # in upstream releases and easy to misread, so use the explicit form.
        run_streamed(["virtnbdrestore", "-a", "verify", "-i", str(backup_dir), "-o", str(backup_dir)])
    except CommandError as exc:
        event("error", "verify failed", backup=str(backup_dir), stderr=exc.result.stderr.strip())
        return False
    event("info", "verify passed", backup=str(backup_dir))
    return True


def verify(config: Config, vm_name: str | None = None) -> int:
    root = backup_root(config)
    backup_path = config.path_value("BACKUP_PATH")
    roots, name_ok = _iter_verify_targets(root, vm_name)
    ok = name_ok
    verified = 0
    for vm_root in roots:
        if not subpath_is_safe(backup_path, vm_root):
            event("error", "verify skipped because path is unsafe", path=str(vm_root))
            ok = False
            continue
        if not vm_root.is_dir():
            if vm_name:
                event("error", "verify target not found", vm=vm_name, path=str(vm_root))
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
    if verified == 0:
        event("error", "verify found no backups", vm=vm_name or None, root=str(root))
        ok = False
    return 0 if ok else 1
