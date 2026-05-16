from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .config import prefixed, root_prefix
from .logging_json import event
from .shell import run

RUN_UNIT_NAME = "libvirt-backup-system.service"
CHECK_UNIT_NAME = "libvirt-backup-system-check.service"
TIMER_UNIT_NAME = "libvirt-backup-system.timer"
STATUS_UNITS = (TIMER_UNIT_NAME, RUN_UNIT_NAME, CHECK_UNIT_NAME)
UNIT_DESCRIPTIONS = {"run": "Libvirt VM backup orchestrator", "check": "Libvirt VM backup preflight check"}
DISPATCH_OPT_OUT_ENV = "LIBVIRT_BACKUP_NO_SYSTEMD_DISPATCH"


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
Description={description}
Wants=network-online.target
After=network-online.target libvirtd.service
{requires_mounts_for}
[Service]
Type=oneshot
TimeoutStartSec=infinity
EnvironmentFile={environment_file}
ExecStart={bin_path} --config {config_arg} {subcommand}
# Defense-in-depth hardening. The service runs as root because it shells out to
# virsh/virtnbdbackup against qemu:///system; the remaining directives close
# the easy kernel-surface escalation paths without sandboxing the filesystem,
# which would hide VM disk roots and backup destinations placed under /home.
# StateDirectory= creates /var/lib/libvirt-backup-system at service start so
# lock.py's run-lock mkdir succeeds on a fresh install.
StateDirectory=libvirt-backup-system
NoNewPrivileges=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
LockPersonality=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
"""

UNIT_TIMER = """[Unit]
Description=Run libvirt VM backups on schedule

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


_SYSTEMD_PATH_FORBIDDEN_CHARS = frozenset("`'\"\\")


def validate_systemd_path(value: str | Path, label: str) -> str:
    path = str(value)
    if not Path(path).is_absolute():
        raise ValueError(f"{label} must be an absolute path for systemd units: {path}")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError(f"{label} must not contain control characters for systemd units")
    # Reject characters that change the shape of ExecStart= rendering even
    # under _quote_systemd_path: backticks (no shell expansion at exec time,
    # but operator scripts and logging pipelines often re-render the line
    # through a shell), backslashes (continuation/escape), and the quote
    # characters we use to wrap the value. ``$`` and ``%`` are still handled
    # by _quote_systemd_path's $$/%% escapes because we own that escape path;
    # the chars listed here are ones we refuse outright at install time so a
    # quoting bug can never silently expand them.
    bad = sorted({char for char in path if char in _SYSTEMD_PATH_FORBIDDEN_CHARS})
    if bad:
        raise ValueError(f"{label} must not contain {''.join(bad)!r} for systemd units")
    return path


def _quote_systemd_path(path: str) -> str:
    # For ExecStart= (the only command-line directive we render), systemd parses
    # with quote handling, so wrap the value in double quotes and escape %
    # (specifier expansion) and $ (env-var expansion when EnvironmentFile= is
    # loaded).
    escaped = path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _escape_systemd_path(path: str) -> str:
    # For path-typed directives (EnvironmentFile=, RequiresMountsFor=), systemd
    # does not strip surrounding quotes — a quoted value is rejected as
    # "path is not absolute". Emit unquoted, backslash-escape whitespace, and
    # double % to dodge specifier expansion.
    return path.replace("\\", "\\\\").replace("%", "%%").replace("\t", "\\\t").replace(" ", "\\ ")


def render_unit_service(backup_path: str, bin_path: Path, config_path: Path, *, subcommand: str = "run") -> str:
    if subcommand not in UNIT_DESCRIPTIONS:
        raise ValueError(f"unknown unit subcommand: {subcommand}")
    backup_path = backup_path.strip()
    config = validate_systemd_path(config_path, "config_path")
    binary = validate_systemd_path(bin_path, "bin_path")
    # TimeoutStartSec is fixed at infinity because a single run backs up every
    # selected VM sequentially. shell.run/run_streamed enforces
    # COMMAND_TIMEOUT_SECONDS per child process, which is the meaningful safety
    # net; a static systemd timeout would either kill legitimate multi-VM runs
    # or be so large it adds no value.
    validate_systemd_path(backup_path, "BACKUP_PATH")
    requires = f"RequiresMountsFor={_escape_systemd_path(backup_path)}\n" if backup_path else ""
    return UNIT_SERVICE.format(
        description=UNIT_DESCRIPTIONS[subcommand],
        requires_mounts_for=requires,
        bin_path=_quote_systemd_path(binary),
        environment_file=_escape_systemd_path(config),
        config_arg=_quote_systemd_path(config),
        subcommand=subcommand,
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


def unit_name_for(subcommand: str) -> str:
    if subcommand == "run":
        return RUN_UNIT_NAME
    if subcommand == "check":
        return CHECK_UNIT_NAME
    raise ValueError(f"no dispatch unit for subcommand: {subcommand}")


def dispatch_via_systemd(
    subcommand: str,
    *,
    prefix: str | None,
    config_path: str | None,
) -> int | None:
    """Run ``subcommand`` through the installed systemd unit.

    Returns the unit's exit code when dispatch is taken, or ``None`` when the
    caller should fall back to running the subcommand in-process. Falling back
    is the right thing whenever dispatch would change semantics:

    - ``INVOCATION_ID`` is set: we are already executing inside the unit and
      dispatching again would loop forever.
    - ``LIBVIRT_BACKUP_NO_SYSTEMD_DISPATCH`` is set: explicit operator opt-out
      for development or recovery.
    - ``--prefix`` is set: install rooted elsewhere; systemctl on this host
      manages a different (the real ``/``) install.
    - ``--config`` is set: the unit has a config path baked into ``ExecStart``;
      honoring a different path means staying in-process.
    - No systemctl available, or the unit file is not on disk yet.
    """
    if os.environ.get("INVOCATION_ID"):
        return None
    if os.environ.get(DISPATCH_OPT_OUT_ENV):
        return None
    if prefix is not None or config_path is not None:
        return None
    root = root_prefix(prefix)
    if not systemctl_available(root):
        return None
    unit = unit_name_for(subcommand)
    if not (prefixed("/etc/systemd/system", root) / unit).exists():
        return None
    event("info", "dispatching to systemd unit", unit=unit, subcommand=subcommand)
    rc = _await_unit(unit)
    # ``systemctl show`` returns the most-recent invocation id even after the
    # unit has finished — filter the journal to just this run's output so the
    # operator sees exactly what the unit logged, with no surrounding noise.
    inv = subprocess.run(
        ["systemctl", "show", unit, "--property=InvocationID", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    inv_id = inv.stdout.strip()
    if inv_id:
        subprocess.run(
            ["journalctl", f"_SYSTEMD_INVOCATION_ID={inv_id}", "--output=cat", "--no-pager"],
            check=False,
            stdout=sys.stderr,
        )
    return rc


def _await_unit(unit: str) -> int:
    """Run ``systemctl start --wait`` and forward Ctrl-C to the unit.

    A bare ``systemctl start --wait`` propagates SIGINT to its own process but
    not to the unit it is waiting on — the operator's Ctrl-C returns 130 to
    the shell while the unit and the held run lock keep running. Install a
    handler that issues ``systemctl stop --no-block`` so the unit is asked to
    stop in the background; we then keep waiting until systemctl returns so
    the exit code reflects the unit's real outcome (stopped, killed, etc.)
    instead of the partial 130.
    """
    previous = signal.getsignal(signal.SIGINT)

    def _forward(_signum: int, _frame: object) -> None:
        with contextlib.suppress(OSError):
            subprocess.run(["systemctl", "stop", "--no-block", unit], check=False)
        event("info", "forwarded SIGINT to systemd unit via stop --no-block", unit=unit)

    signal.signal(signal.SIGINT, _forward)
    try:
        return subprocess.run(["systemctl", "start", "--wait", unit], check=False).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
