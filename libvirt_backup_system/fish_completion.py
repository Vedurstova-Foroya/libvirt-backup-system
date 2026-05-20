"""Install and remove the fish shell completion file for libvirt-backup-system.

The completion script itself lives in the package as
``libvirt_backup_system/data/libvirt-backup-system.fish``. Install copies it
to ``/usr/share/fish/vendor_completions.d/`` (the standard
vendor-installed location fish auto-loads regardless of whether the user has
sourced anything); uninstall removes it. Both operations are best-effort and
never abort the surrounding install/uninstall when fish is not present.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .config import prefixed
from .logging_json import event

# Standard system path fish reads at startup. We deliberately do NOT use
# /etc/fish/completions/ because that directory is reserved for the local
# admin's per-host edits; vendor_completions.d is where packages drop their
# bundled completions. Fish auto-loads from this path even when fish itself
# is not installed (the dir is sometimes precreated by the fish package), so
# we create it if missing rather than gating on its presence.
FISH_COMPLETION_DIR = Path("/usr/share/fish/vendor_completions.d")
FISH_COMPLETION_NAME = "libvirt-backup-system.fish"


def _packaged_completion_path() -> Path:
    return Path(__file__).resolve().parent / "data" / FISH_COMPLETION_NAME


def fish_completion_target(root: Path) -> Path:
    return prefixed(FISH_COMPLETION_DIR / FISH_COMPLETION_NAME, root)


def install_fish_completion(root: Path) -> None:
    """Copy the bundled fish completion script under ``root``.

    Failures are logged at ``warning`` and swallowed: a missing fish, a
    read-only /usr/share, or a hostile filesystem must not abort the rest of
    the install. The CLI works without completion; only TAB-driven discovery
    is degraded.
    """
    source = _packaged_completion_path()
    if not source.is_file():
        event("warning", "fish completion source missing in package", path=str(source))
        return
    target = fish_completion_target(root)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    except OSError as exc:
        event("warning", "fish completion install skipped", path=str(target), error=str(exc))
        return
    event("info", "installed fish completion", path=str(target))


def remove_fish_completion(root: Path) -> bool:
    """Delete the previously installed fish completion script.

    Returns ``True`` when the file is gone (already absent or removed
    successfully) and ``False`` when a real OSError prevents removal. Used by
    uninstall; mirrors the (bool ok) return shape of the surrounding cleanup
    helpers so the overall exit code can fold this in cleanly.
    """
    target = fish_completion_target(root)
    try:
        target.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        event("error", "failed to remove fish completion", path=str(target), error=str(exc))
        return False
    event("info", "removed fish completion", path=str(target))
    return True
