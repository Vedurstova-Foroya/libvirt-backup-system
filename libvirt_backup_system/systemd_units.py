from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import systemd_render
from .config import bool_value, prefixed, root_prefix
from .logging_json import event
from .shell import run
from .systemd_run_gate import manual_run_ready

RUN_UNIT_NAME = "libvirt-backup-system.service"
CHECK_UNIT_NAME = "libvirt-backup-system-check.service"
TIMER_UNIT_NAME = "libvirt-backup-system.timer"
MAINTENANCE_UNIT_NAME = "libvirt-backup-system-maintenance.service"
MAINTENANCE_TIMER_NAME = "libvirt-backup-system-maintenance.timer"
MAINTENANCE_FULL_UNIT_NAME = "libvirt-backup-system-maintenance-full.service"
MAINTENANCE_FULL_TIMER_NAME = "libvirt-backup-system-maintenance-full.timer"
VERIFY_UNIT_NAME = "libvirt-backup-system-verify.service"
VERIFY_TIMER_NAME = "libvirt-backup-system-verify.timer"
STATUS_UNITS = (
    TIMER_UNIT_NAME,
    RUN_UNIT_NAME,
    CHECK_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)
UNIT_DESCRIPTIONS = systemd_render.UNIT_DESCRIPTIONS
KOPIA_UNIT_DESCRIPTIONS = systemd_render.KOPIA_UNIT_DESCRIPTIONS
# Quick maintenance runs on the configured daily-ish cadence; full
# maintenance is scheduled separately weekly for GC. Verify performs a 1%
# files probe weekly.
KOPIA_UNIT_ARGS = systemd_render.KOPIA_UNIT_ARGS
KOPIA_FULL_MAINTENANCE_INTERVAL = systemd_render.KOPIA_FULL_MAINTENANCE_INTERVAL
KOPIA_TIMER_ON_ACTIVE_SEC = systemd_render.KOPIA_TIMER_ON_ACTIVE_SEC
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
        worst = max(worst, _status_returncode(unit, result.returncode))
    return worst


def _status_returncode(unit: str, status_returncode: int) -> int:
    if status_returncode != 3:
        return status_returncode
    result = subprocess.run(
        ["systemctl", "show", unit, "--property=LoadState", "--property=ActiveState", "--value"],
        check=False,
        capture_output=True,
        text=True,
    )
    values = result.stdout.splitlines()
    if result.returncode == 0 and values[:2] == ["loaded", "inactive"]:
        return 0
    return status_returncode


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


def validate_systemd_path(value: str | Path, label: str) -> str:
    return systemd_render.validate_systemd_path(value, label)


def quote_systemd_path(path: str) -> str:
    return systemd_render.quote_systemd_path(path)


def escape_systemd_path(path: str) -> str:
    return systemd_render.escape_systemd_path(path)


def requires_mounts_for(backup_path: str) -> str:
    return systemd_render.requires_mounts_for(backup_path)


def render_unit_service(backup_path: str, bin_path: Path, config_path: Path, *, subcommand: str = "run") -> str:
    return systemd_render.render_unit_service(backup_path, bin_path, config_path, subcommand=subcommand)


def has_control_char(value: str) -> bool:
    return systemd_render.has_control_char(value)


def _systemd_analyze_available(root: Path) -> bool:
    return root == Path("/") and Path("/run/systemd/system").exists() and bool(shutil.which("systemd-analyze"))


def render_unit_timer(root: Path, calendar: str) -> str | None:
    return systemd_render.render_unit_timer(
        root,
        calendar,
        analyze_available=_systemd_analyze_available,
        run_command=run,
    )


def render_unit_kopia_service(bin_path: Path, config_path: Path, *, kind: str, backup_path: str = "") -> str:
    return systemd_render.render_unit_kopia_service(bin_path, config_path, kind=kind, backup_path=backup_path)


def render_unit_interval_timer(*, description: str, interval: str, on_active_sec: str = "15min") -> str | None:
    return systemd_render.render_unit_interval_timer(
        description=description, interval=interval, on_active_sec=on_active_sec
    )


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
    if bool_value(os.environ.get(DISPATCH_OPT_OUT_ENV, "")):
        return None
    if prefix is not None or config_path is not None:
        return None
    root = root_prefix(prefix)
    if not systemctl_available(root):
        return None
    unit = unit_name_for(subcommand)
    if not (prefixed("/etc/systemd/system", root) / unit).exists():
        if subcommand == "run":
            event(
                "error",
                "backup service is not running; run start before run",
                unit=unit,
                timer=TIMER_UNIT_NAME,
            )
            return 1
        return None
    if subcommand == "run" and not manual_run_ready(root, run_unit_name=RUN_UNIT_NAME, timer_unit_name=TIMER_UNIT_NAME):
        return 1
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
    if subcommand == "check" and rc == 0:
        event("info", "check passed", unit=unit)
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
