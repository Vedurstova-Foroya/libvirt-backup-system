from __future__ import annotations

from pathlib import Path

from .config import Config, prefixed
from .logging_json import event

HOST_ID_STATE_FILE = "host-id"


def validation_failure(host_id: str, *, label: str = "HOST_ID", allow_empty: bool = False) -> str | None:
    if not host_id.strip():
        return None if allow_empty else f"{label} must not be empty"
    if host_id in {".", ".."} or "/" in host_id or "\\" in host_id:
        return f"{label} must not contain path separators or be '.'/'..'"
    if any(ord(c) < 32 or ord(c) == 127 for c in host_id):
        return f"{label} must not contain control characters or NUL"
    if host_id != host_id.strip():
        return f"{label} must not have leading or trailing whitespace"
    if any(c.isspace() for c in host_id):
        return f"{label} must not contain whitespace"
    return None


def host_id_state_path(config: Config) -> Path:
    return prefixed("/var/lib/libvirt-backup-system", config.prefix) / HOST_ID_STATE_FILE


def stamp_host_id_on_first_run(config: Config) -> list[str]:
    path = host_id_state_path(config)
    host_id = config.get("HOST_ID")
    try:
        if path.exists():
            stamped = path.read_text(encoding="utf-8").strip()
            if stamped and stamped != host_id:
                return [f"HOST_ID drift detected: state has {stamped!r}, config has {host_id!r}"]
            if not stamped:
                path.write_text(host_id + "\n", encoding="utf-8")
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(host_id + "\n", encoding="utf-8")
        event("info", "stamped HOST_ID state", path=str(path), host_id=host_id)
    except OSError as exc:
        return [f"HOST_ID state check failed: {exc}"]
    return []


def host_id_drift_failures(config: Config) -> list[str]:
    path = host_id_state_path(config)
    try:
        if not path.exists():
            return []
        stamped = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"HOST_ID state check failed: {exc}"]
    if stamped and stamped != config.get("HOST_ID"):
        return [f"HOST_ID drift detected: state has {stamped!r}, config has {config.get('HOST_ID')!r}"]
    return []
