from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .logging_json import event
from .shell import CommandResult
from .systemd_templates import UNIT_INTERVAL_TIMER, UNIT_KOPIA_SERVICE, UNIT_SERVICE, UNIT_TIMER

UNIT_DESCRIPTIONS = {"run": "Libvirt VM backup orchestrator", "check": "Libvirt VM backup preflight check"}
KOPIA_UNIT_DESCRIPTIONS = {
    "maintenance": "Libvirt VM backup kopia maintenance",
    "maintenance-full": "Libvirt VM backup kopia full maintenance",
    "verify": "Libvirt VM backup kopia snapshot verify",
}
KOPIA_UNIT_ARGS = {
    "maintenance": "maintenance run --safety=full",
    "maintenance-full": "maintenance run --safety=full --full",
    "verify": "snapshot verify --max-failures=0 --verify-files-percent=1",
}
KOPIA_FULL_MAINTENANCE_INTERVAL = "7d"
_SYSTEMD_PATH_FORBIDDEN_CHARS = frozenset("`'\"\\")


def validate_systemd_path(value: str | Path, label: str) -> str:
    path = str(value)
    if not Path(path).is_absolute():
        raise ValueError(f"{label} must be an absolute path for systemd units: {path}")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError(f"{label} must not contain control characters for systemd units")
    bad = sorted({char for char in path if char in _SYSTEMD_PATH_FORBIDDEN_CHARS})
    if bad:
        raise ValueError(f"{label} must not contain {''.join(bad)!r} for systemd units")
    return path


def quote_systemd_path(path: str) -> str:
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def escape_systemd_path(path: str) -> str:
    return path.replace("\\", "\\\\").replace("%", "%%").replace("\t", "\\\t").replace(" ", "\\ ")


def requires_mounts_for(backup_path: str) -> str:
    backup_path = backup_path.strip()
    if not backup_path:
        return ""
    validate_systemd_path(backup_path, "BACKUP_PATH")
    return f"RequiresMountsFor={escape_systemd_path(backup_path)}\n"


def render_unit_service(backup_path: str, bin_path: Path, config_path: Path, *, subcommand: str = "run") -> str:
    if subcommand not in UNIT_DESCRIPTIONS:
        raise ValueError(f"unknown unit subcommand: {subcommand}")
    backup_path = backup_path.strip()
    config = validate_systemd_path(config_path, "config_path")
    binary = validate_systemd_path(bin_path, "bin_path")
    return UNIT_SERVICE.format(
        description=UNIT_DESCRIPTIONS[subcommand],
        requires_mounts_for=requires_mounts_for(backup_path),
        bin_path=quote_systemd_path(binary),
        environment_file=escape_systemd_path(config),
        config_arg=quote_systemd_path(config),
        subcommand=subcommand,
    )


def has_control_char(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def render_unit_timer(
    root: Path,
    calendar: str,
    *,
    analyze_available: Callable[[Path], bool],
    run_command: Callable[..., CommandResult],
) -> str | None:
    if has_control_char(calendar):
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not contain control characters")
        return None
    calendar = calendar.strip()
    if not calendar:
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not be empty")
        return None
    if calendar.startswith("-"):
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not start with '-'")
        return None
    if analyze_available(root):
        result = run_command(["systemd-analyze", "calendar", calendar], check=False)
        if result.returncode != 0:
            event(
                "error",
                "invalid systemd calendar",
                calendar=calendar,
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
            return None
    return UNIT_TIMER.format(calendar=calendar)


def render_unit_kopia_service(bin_path: Path, config_path: Path, *, kind: str, backup_path: str = "") -> str:
    if kind not in KOPIA_UNIT_DESCRIPTIONS:
        raise ValueError(f"unknown kopia unit kind: {kind}")
    config = validate_systemd_path(config_path, "config_path")
    binary = validate_systemd_path(bin_path, "bin_path")
    return UNIT_KOPIA_SERVICE.format(
        description=KOPIA_UNIT_DESCRIPTIONS[kind],
        requires_mounts_for=requires_mounts_for(backup_path),
        bin_path=quote_systemd_path(binary),
        environment_file=escape_systemd_path(config),
        config_arg=quote_systemd_path(config),
        kopia_args=KOPIA_UNIT_ARGS[kind],
    )


def render_unit_interval_timer(*, description: str, interval: str) -> str | None:
    interval = interval.strip()
    if not interval:
        event("error", "invalid systemd interval", error="timer interval must not be empty")
        return None
    if has_control_char(interval):
        event("error", "invalid systemd interval", error="timer interval must not contain control characters")
        return None
    if interval.startswith("-"):
        event("error", "invalid systemd interval", error="timer interval must not start with '-'")
        return None
    return UNIT_INTERVAL_TIMER.format(description=description, interval=interval)
