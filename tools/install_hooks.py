from __future__ import annotations

import shutil
import stat
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK_SOURCE = ROOT / "tools/hooks/pre-push"
HOOK_TARGET = ROOT / ".git/hooks/pre-push"


def main() -> int:
    HOOK_TARGET.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HOOK_SOURCE, HOOK_TARGET)
    HOOK_TARGET.chmod(HOOK_TARGET.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"installed {HOOK_TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
