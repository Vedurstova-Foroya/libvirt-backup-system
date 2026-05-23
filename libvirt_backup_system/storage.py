from __future__ import annotations

from pathlib import Path


def subpath_is_safe(root: Path, path: Path) -> bool:
    # This is a check, not a lock: every caller mutates the path immediately
    # after (mkdir, kopia repo writes, rename). A sufficiently fast attacker
    # with write access to an intermediate directory could swap a component
    # into a symlink between the check and the syscall. In practice every
    # intermediate directory under BACKUP_PATH/HOST_ID/kopia-repo/ is
    # root-owned on a mount the local user cannot write to, so the attack
    # window is theoretical; closing it would require openat()-style descended
    # traversal which Python does not expose ergonomically.
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False
    if not relative_parts:
        return False

    current = root
    for part in relative_parts:
        current /= part
        if current.is_symlink():
            return False

    return resolved_path_is_within(root, path)


def resolved_path_is_within(parent: Path, path: Path) -> bool:
    parent_resolved = parent.resolve()
    path_resolved = path.resolve(strict=False)
    return path_resolved == parent_resolved or path_resolved.is_relative_to(parent_resolved)
