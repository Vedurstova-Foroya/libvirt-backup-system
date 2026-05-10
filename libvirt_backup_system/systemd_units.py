from __future__ import annotations

import shutil
from pathlib import Path

from .logging_json import event
from .shell import run

UNIT_SERVICE = """[Unit]
Description=Libvirt VM backup orchestrator
Wants=network-online.target
After=network-online.target libvirtd.service
{requires_mounts_for}
[Service]
Type=oneshot
TimeoutStartSec=infinity
EnvironmentFile={environment_file}
ExecStart={bin_path} --config {config_arg} run
"""

UNIT_TIMER = """[Unit]
Description=Run libvirt VM backups on schedule

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


def validate_systemd_path(value: str | Path, label: str) -> str:
    path = str(value)
    if not Path(path).is_absolute():
        raise ValueError(f"{label} must be an absolute path for systemd units: {path}")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError(f"{label} must not contain control characters for systemd units")
    return path


def _quote_systemd_path(path: str, *, escape_dollar: bool = False) -> str:
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if escape_dollar:
        escaped = escaped.replace("$", "$$")
    return f'"{escaped}"'


def render_unit_service(backup_path: str, bin_path: Path, config_path: Path) -> str:
    backup_path = backup_path.strip()
    config = validate_systemd_path(config_path, "config_path")
    binary = validate_systemd_path(bin_path, "bin_path")
    # TimeoutStartSec is fixed at infinity because a single run backs up every
    # selected VM sequentially. shell.run/run_streamed enforces
    # COMMAND_TIMEOUT_SECONDS per child process, which is the meaningful safety
    # net; a static systemd timeout would either kill legitimate multi-VM runs
    # or be so large it adds no value.
    requires = (
        f"RequiresMountsFor={_quote_systemd_path(validate_systemd_path(backup_path, 'BACKUP_PATH'))}\n"
        if backup_path
        else ""
    )
    return UNIT_SERVICE.format(
        requires_mounts_for=requires,
        bin_path=_quote_systemd_path(binary, escape_dollar=True),
        environment_file=_quote_systemd_path(config),
        config_arg=_quote_systemd_path(config, escape_dollar=True),
    )


def _has_control_char(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _systemd_analyze_available(root: Path) -> bool:
    return root == Path("/") and Path("/run/systemd/system").exists() and bool(shutil.which("systemd-analyze"))


def render_unit_timer(root: Path, calendar: str) -> str | None:
    if _has_control_char(calendar):
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not contain control characters")
        return None
    calendar = calendar.strip()
    if not calendar:
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not be empty")
        return None
    if _systemd_analyze_available(root):
        result = run(["systemd-analyze", "calendar", calendar], check=False)
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
