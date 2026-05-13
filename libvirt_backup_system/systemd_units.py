from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import prefixed, root_prefix
from .logging_json import event
from .shell import run

STATUS_UNITS = ("libvirt-backup-system.timer", "libvirt-backup-system.service")


def systemctl_available(root: Path) -> bool:
    return root == Path("/") and Path("/run/systemd/system").exists() and bool(shutil.which("systemctl"))


def status(prefix: str | None = None) -> int:
    root = root_prefix(prefix)
    if not systemctl_available(root):
        event("error", "systemctl unavailable; install systemd or run on a systemd host")
        return 1
    # No capture: ``status`` is a human-facing summary, not a logged event,
    # so let systemctl's pager-less output flow straight to the user's tty.
    worst = 0
    for unit in STATUS_UNITS:
        result = subprocess.run(["systemctl", "status", "--no-pager", unit], check=False)
        worst = max(worst, result.returncode)
    return worst


def run_systemctl(root: Path, commands: list[list[str]]) -> bool:
    if not systemctl_available(root):
        return True
    systemd_dir = prefixed("/etc/systemd/system", root)
    all_ok = True
    for args in commands:
        # Skip ``disable``/``stop`` of units that were never installed (fresh
        # host) or have already been removed (re-running uninstall). systemctl
        # otherwise exits nonzero with "Unit X does not exist", which would
        # make install/uninstall non-idempotent. ``enable`` and
        # ``daemon-reload`` are always run.
        if len(args) >= 2 and args[1] in {"disable", "stop"}:
            unit_name = args[-1]
            if not (systemd_dir / unit_name).exists():
                event(
                    "info",
                    f"systemctl {args[1]} skipped because unit file is absent",
                    unit=unit_name,
                    path=str(systemd_dir / unit_name),
                )
                continue
        result = run(args, check=False)
        if result.returncode != 0:
            event(
                "error",
                f"{' '.join(args)} failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
            all_ok = False
    return all_ok


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
# Defense-in-depth hardening. The service still runs as root because it shells
# out to virsh/virtnbdbackup against qemu:///system, but the rest of the
# filesystem is read-only and most kernel-surface escalation paths are closed.
# Operators who need to widen access can drop a unit override under
# /etc/systemd/system/libvirt-backup-system.service.d/.
# StateDirectory= creates /var/lib/libvirt-backup-system before the sandbox is
# applied and includes it in the writable set automatically. Without it, a
# fresh install would fail at unit start because the run-lock directory did
# not exist when ReadWritePaths= tried to bind it.
StateDirectory=libvirt-backup-system
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
ReadWritePaths={read_write_paths}
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
    # ReadWritePaths must include BACKUP_PATH (the backup destination),
    # /var/lib/libvirt-backup-system (the run lock and runtime state), and
    # /var/tmp (virtnbdbackup writes scratch files there by default; the
    # default is unaffected by ``--scratchdir`` and ProtectSystem=strict
    # otherwise mounts /var/tmp read-only, which breaks scheduled runs while
    # leaving manual invocations on the unsandboxed shell succeeding).
    read_write_entries = [_quote_systemd_path(validate_systemd_path(backup_path, "BACKUP_PATH"))]
    read_write_entries.append(_quote_systemd_path("/var/lib/libvirt-backup-system"))
    read_write_entries.append(_quote_systemd_path("/var/tmp"))  # noqa: S108 - virtnbdbackup scratch.
    return UNIT_SERVICE.format(
        requires_mounts_for=requires,
        bin_path=_quote_systemd_path(binary, escape_dollar=True),
        environment_file=_quote_systemd_path(config),
        config_arg=_quote_systemd_path(config, escape_dollar=True),
        read_write_paths=" ".join(read_write_entries),
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
    if calendar.startswith("-"):
        # ``systemd-analyze calendar --help`` exits 0, so a typo'd ``--help``
        # would pass the rc check and render a unit file that systemctl
        # daemon-reload then refuses. Catch the obvious flag-shaped value here.
        event("error", "invalid systemd calendar", error="SYSTEMD_ON_CALENDAR must not start with '-'")
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
