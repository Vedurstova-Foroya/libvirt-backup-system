"""Share the env config across hosts through the backup tree.

A single "shared config" file lives at ``BACKUP_PATH/<SHARED_CONFIG_NAME>``,
alongside the per-host ``BACKUP_PATH/<host-id>/kopia-repo/`` directories. It is
a *seed*, not a live-synced file:

* The first node publishes its config there (``install``/``start``, and the
  explicit ``update-config``).
* A node *joining* an existing backup tree pulls that seed as its initial
  local config, so it inherits retention, splitter, NFS policy, etc. without
  re-typing them.
* After joining, the local config is independent. Editing it does not touch
  the seed; the seed only changes when someone runs ``update-config``.

``HOST_ID`` is deliberately blanked in the seed: host identity scopes the
per-host repo (``BACKUP_PATH/<HOST_ID>/kopia-repo/``), so sharing it would
collide two hosts onto one repo. Each node falls back to ``/etc/machine-id``.
"""

from __future__ import annotations

import os
from pathlib import Path

from .config import Config, parse_env_file
from .config_data import DEFAULTS
from .logging_json import event

SHARED_CONFIG_NAME = "libvirt-backup.env"


def shared_config_path(config: Config) -> Path | None:
    """Path of the shared seed under ``BACKUP_PATH``, or ``None`` if unset.

    ``BACKUP_PATH`` is an operator-supplied mount path used verbatim (it is not
    run through the install ``--prefix``), matching ``paths.backup_root`` and
    the per-host repo layout.
    """
    backup_path = config.get("BACKUP_PATH").strip()
    if not backup_path:
        return None
    return Path(backup_path) / SHARED_CONFIG_NAME


def _render_seed(source_path: Path) -> str:
    """Render the seed env text from a local env file.

    Re-renders through ``Config.render_env`` so the published file is the clean,
    fully-commented template form regardless of how the source was edited, and
    forces ``HOST_ID`` empty so joiners never inherit another host's identity.
    """
    values = dict(DEFAULTS)
    values.update(parse_env_file(source_path))
    values["HOST_ID"] = ""
    return Config(values=values, path=source_path, prefix=Path("/")).render_env()


def _atomic_write(dest: Path, content: str) -> None:
    # Temp-in-same-dir + atomic rename so a peer reading the seed over NFS never
    # observes a half-written file. 0600: the seed mirrors the local env, which
    # is root-owned 0600; backup-tree access is already root-on-every-host.
    tmp = dest.with_name(f".{dest.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(dest)


def _exclusive_write(dest: Path, content: str) -> bool:
    """Create ``dest`` only if absent. Returns ``False`` when it already exists.

    O_EXCL closes the race where two first-time installs target the same shared
    backup tree concurrently: the first writer wins, the rest see the seed and
    fall through to the join path instead of clobbering it.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(dest, flags, 0o600)
    except FileExistsError:
        return False
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def pull_shared_config_values(config: Config) -> dict[str, str] | None:
    """Parsed seed values for a joining node, or ``None`` when no seed exists.

    Returns the env key/value mapping (``HOST_ID`` is never present — it is
    blanked/commented in the seed) so the caller can overlay it onto the
    install-time config before rendering the local env file.
    """
    src = shared_config_path(config)
    if src is None or not src.exists():
        return None
    try:
        return parse_env_file(src)
    except OSError as exc:
        event("warning", "shared config unreadable; using defaults", path=str(src), error=str(exc))
        return None


def seed_shared_config(config: Config, source_path: Path) -> None:
    """Best-effort publish of the first node's config to the backup tree.

    Used by ``install``/``start``: writes the seed only if it does not already
    exist, so a node that joined later (and edited its own config) never
    clobbers the shared template. Failures are warnings, not errors — config
    sharing is a convenience and must not fail an otherwise-good install.
    """
    dest = shared_config_path(config)
    if dest is None:
        return
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if _exclusive_write(dest, _render_seed(source_path)):
            event("info", "published shared config", path=str(dest))
    except OSError as exc:
        event("warning", "failed to publish shared config", path=str(dest), error=str(exc))


def update_shared_config(config: Config) -> int:
    """Overwrite the shared seed with this host's current config.

    Backs the ``update-config`` command. Unlike ``seed_shared_config`` this
    always replaces the seed (last writer wins), so a node that joins after
    this call inherits this host's settings. Returns a process exit code.
    """
    dest = shared_config_path(config)
    if dest is None:
        event("error", "BACKUP_PATH is not configured; set it before update-config")
        return 1
    if not config.path.exists():
        event("error", "config file not found", path=str(config.path))
        return 1
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(dest, _render_seed(config.path))
    except OSError as exc:
        event("error", "failed to publish shared config", path=str(dest), error=str(exc))
        return 1
    event("info", "published shared config", path=str(dest))
    return 0
