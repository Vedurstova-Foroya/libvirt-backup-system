from __future__ import annotations

import argparse
import datetime as dt
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = ROOT / "tools/hooks/pre-push"
HOOK_TARGET = ROOT / ".git/hooks/pre-push"


def _install(target: Path, *, force: bool) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_bytes = HOOK_SOURCE.read_bytes()
    if target.exists() and target.read_bytes() != source_bytes and not force:
        # A developer may already have a personal pre-push hook (chained gates,
        # ticket-id check, etc.). Refuse to clobber it silently; require
        # ``--force`` and stash a timestamped backup so they can restore it.
        backup = target.with_name(f"{target.name}.bak.{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}")
        print(
            f"refusing to overwrite existing {target.relative_to(ROOT)}: "
            f"contents differ from {HOOK_SOURCE.relative_to(ROOT)}. "
            f"Re-run with --force (a backup will be written to {backup.relative_to(ROOT)}) "
            f"or remove the file first.",
            file=sys.stderr,
        )
        return 1
    if target.exists() and target.read_bytes() != source_bytes and force:
        backup = target.with_name(f"{target.name}.bak.{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}")
        shutil.copy2(target, backup)
        print(f"backed up existing hook to {backup.relative_to(ROOT)}")
    shutil.copy2(HOOK_SOURCE, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {target.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the repository pre-push hook.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing pre-push hook after backing it up.")
    args = parser.parse_args(argv)
    return _install(HOOK_TARGET, force=args.force)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
