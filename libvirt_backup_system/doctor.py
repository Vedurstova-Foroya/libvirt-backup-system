from __future__ import annotations

import os
from pathlib import Path

from .config import Config, prefixed
from .logging_json import event
from .preflight import collect_check_failures, host_id_drift_failures
from .shell import run
from .systemd_units import (
    CHECK_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    render_unit_service,
    render_unit_timer,
    systemctl_available,
)

WRAPPER_PATH = "/usr/local/bin/libvirt-backup-system"
PACKAGE_PATH = "/opt/libvirt-backup-system/libvirt_backup_system"
SYSTEMD_DIR = "/etc/systemd/system"
# Anything outside this set (exit-code, signal, timeout, oom-kill, core-dump,
# watchdog, ...) means the most-recent run did not complete cleanly. The
# ``success`` value also fires before the first run, so the timer
# LastTriggerUSec is consulted alongside ``Result`` to tell "never fired" apart
# from "succeeded".
HEALTHY_RUN_RESULTS = frozenset({"", "success"})
# Properties systemd returns as the all-zeros value when the timer has not
# fired yet or has no future trigger scheduled. ``0`` is the literal stringy
# answer from ``systemctl show --value`` on a fresh install.
NEVER_FIRED_PROPERTY_VALUES = frozenset({"", "0"})


def _check_wrapper(root: Path) -> list[str]:
    bin_path = prefixed(WRAPPER_PATH, root)
    if not bin_path.is_file():
        return [f"wrapper script missing: {bin_path}; run install"]
    if not os.access(bin_path, os.X_OK):
        return [f"wrapper script not executable: {bin_path}; re-run install"]
    return []


def _check_package(root: Path) -> list[str]:
    package_dst = prefixed(PACKAGE_PATH, root)
    if not package_dst.is_dir():
        return [f"package directory missing: {package_dst}; run install"]
    return []


def _check_config_file(config: Config) -> list[str]:
    if not config.path.is_file():
        return [f"config file missing: {config.path}; run install"]
    return []


def _expected_unit_text(config: Config, name: str) -> str | None:
    root = config.prefix
    bin_path = prefixed(WRAPPER_PATH, root)
    backup_path = config.get("BACKUP_PATH").strip()
    try:
        if name == RUN_UNIT_NAME:
            return render_unit_service(backup_path, bin_path, config.path, subcommand="run")
        if name == CHECK_UNIT_NAME:
            return render_unit_service(backup_path, bin_path, config.path, subcommand="check")
        return render_unit_timer(root, config.get("SYSTEMD_ON_CALENDAR"))
    except ValueError as exc:
        event("error", "doctor cannot render expected unit", unit=name, error=str(exc))
        return None


def _check_units(config: Config) -> list[str]:
    # install skips writing unit files when BACKUP_PATH is empty; validate_config
    # already flags the empty value, so there is no separate failure to add here.
    # But if the operator hand-edited BACKUP_PATH= back to empty without
    # running start, the previously-installed units stay on disk — flag that as
    # a hint to refresh registration so it matches the new config.
    if not config.get("BACKUP_PATH").strip():
        systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
        stale = [
            str(systemd_dir / name)
            for name in (RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME)
            if (systemd_dir / name).is_file()
        ]
        if stale:
            return [
                "systemd units present but BACKUP_PATH is empty; " f"run start after fixing config: {', '.join(stale)}"
            ]
        return []
    failures: list[str] = []
    systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
    for name in (RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME):
        unit_path = systemd_dir / name
        if not unit_path.is_file():
            failures.append(f"systemd unit missing: {unit_path}; run start")
            continue
        expected = _expected_unit_text(config, name)
        if expected is None:
            failures.append(f"cannot validate {unit_path}: rendering expected unit failed")
            continue
        if unit_path.read_text(encoding="utf-8") != expected:
            failures.append(f"systemd unit out of date: {unit_path}; run start")
    return failures


def _systemctl_value(unit: str, prop: str) -> str:
    result = run(["systemctl", "show", unit, f"--property={prop}", "--value"], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _check_runtime_state(root: Path) -> list[str]:
    if not systemctl_available(root):
        return []
    failures: list[str] = []
    enabled = _systemctl_value(TIMER_UNIT_NAME, "UnitFileState")
    if enabled != "enabled":
        failures.append(f"timer not enabled: {TIMER_UNIT_NAME} UnitFileState={enabled or 'unknown'}; run start")
    active = _systemctl_value(TIMER_UNIT_NAME, "ActiveState")
    if active != "active":
        failures.append(f"timer not active: {TIMER_UNIT_NAME} ActiveState={active or 'unknown'}")
    # NeedDaemonReload catches the case where the unit file on disk matches
    # what install would render today, but systemd is still running an older
    # cached copy because nobody ran ``daemon-reload`` after a hand-edit.
    if _systemctl_value(RUN_UNIT_NAME, "NeedDaemonReload") == "yes":
        failures.append(f"systemd needs daemon-reload: {RUN_UNIT_NAME} cached unit is stale; run start")
    last_result = _systemctl_value(RUN_UNIT_NAME, "Result")
    last_trigger = _systemctl_value(TIMER_UNIT_NAME, "LastTriggerUSec")
    next_elapse = _systemctl_value(TIMER_UNIT_NAME, "NextElapseUSecRealtime")
    if last_trigger in NEVER_FIRED_PROPERTY_VALUES:
        # ``Result=success`` is also the pre-first-fire value. Cross-checking
        # the timer lets doctor say "never fired" instead of falsely claiming
        # the run passed; the next elapse only counts as a green signal when
        # the timer has actually scheduled a future trigger.
        if next_elapse in NEVER_FIRED_PROPERTY_VALUES:
            failures.append(
                f"timer has not fired and no next elapse scheduled: {TIMER_UNIT_NAME}; check systemctl list-timers"
            )
    elif last_result not in HEALTHY_RUN_RESULTS:
        failures.append(f"last run failed: {RUN_UNIT_NAME} Result={last_result}")
    return failures


def doctor(config: Config) -> int:
    # Doctor is a superset of ``check``: it first runs every preflight check
    # (config, binaries, root, VM discovery, scratch dir, NBD probe, backup
    # space) and then appends install/registration/last-run findings. A failure
    # from either layer is reported under the same ``doctor failed`` event so
    # operators see one combined report.
    failures, vm_count, required_kb = collect_check_failures(config)
    failures.extend(_check_config_file(config))
    failures.extend(_check_wrapper(config.prefix))
    failures.extend(_check_package(config.prefix))
    failures.extend(_check_units(config))
    failures.extend(_check_runtime_state(config.prefix))
    failures.extend(host_id_drift_failures(config))
    if failures:
        for failure in failures:
            event("error", "doctor failed", reason=failure)
        return 1
    event("info", "doctor passed", vm_count=vm_count, required_kb=required_kb)
    return 0
