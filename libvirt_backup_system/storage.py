from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path


def subpath_is_safe(root: Path, path: Path) -> bool:
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False

    current = root
    for part in relative_parts:
        current /= part
        if current.is_symlink():
            return False

    return resolved_path_is_within(root, path)


def unsafe_symlink_descendants(root: Path, path: Path) -> Iterator[Path]:
    if not path.exists():
        return

    stack = [path]
    while stack:
        current = stack.pop()
        for child in current.iterdir():
            if child.is_symlink():
                try:
                    child.relative_to(root)
                except ValueError:
                    yield child
                    continue
                if child.is_dir() or not resolved_path_is_within(root, child):
                    yield child
                continue
            if child.is_dir():
                stack.append(child)


def resolved_path_is_within(parent: Path, path: Path) -> bool:
    parent_resolved = parent.resolve()
    path_resolved = path.resolve(strict=False)
    return path_resolved == parent_resolved or path_resolved.is_relative_to(parent_resolved)
