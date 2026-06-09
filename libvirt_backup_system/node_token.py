from __future__ import annotations

import shlex

from . import kopia_password, kopia_repo
from .config import Config
from .logging_json import event


def read_shared_token(config: Config) -> str | None:
    try:
        return kopia_password.read_password_file(config)
    except (OSError, ValueError) as exc:
        event(
            "error",
            "kopia token unreadable",
            error=str(exc),
            password_file=str(kopia_repo.password_file_path(config)),
        )
        return None


def show_token(config: Config) -> int:
    token = read_shared_token(config)
    if token is None:
        return 1
    print(token, flush=True)
    return 0


def add_node(config: Config) -> int:
    backup_path = config.get("BACKUP_PATH").strip()
    if not backup_path:
        event("error", "BACKUP_PATH is not configured; set it before printing an add-node command")
        return 1
    token = read_shared_token(config)
    if token is None:
        return 1
    command = " ".join(
        [
            "sudo",
            "env",
            f"BACKUP_PATH={shlex.quote(backup_path)}",
            f"KOPIA_PW={shlex.quote(token)}",
            "python3",
            "-m",
            "libvirt_backup_system",
            "install",
            "--kopia-password-env",
            "KOPIA_PW",
            "--acknowledge-password-loss",
        ]
    )
    print(command, flush=True)
    return 0
