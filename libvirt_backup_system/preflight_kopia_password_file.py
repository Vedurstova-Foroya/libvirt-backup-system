from __future__ import annotations

from . import kopia_password
from .config import Config, prefixed


def validate_kopia_password_file(config: Config) -> list[str]:
    """Confirm the kopia password file exists and is root-owned mode 600."""
    raw = config.get("KOPIA_PASSWORD_FILE").strip()
    if not raw:
        return ["KOPIA_PASSWORD_FILE must not be empty"]
    path = prefixed(raw, config.prefix)
    failure = kopia_password.password_file_security_failure(path, label="KOPIA_PASSWORD_FILE")
    if failure is not None:
        return [failure]
    return []
