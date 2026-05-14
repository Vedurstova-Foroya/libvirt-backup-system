from __future__ import annotations

import os
from pathlib import Path

from .config import Config, prefixed
from .logging_json import event
from .preflight import collect_check_failures
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
# ``Result`` is ``success`` both before the first run and after a clean run, so
# we cannot distinguish "never fired" from "passed" here. Anything outside this
# set (exit-code, signal, timeout, oom-kill, core-dump, watchdog, ...) means the
# most-recent run did not complete cleanly.
HEALTHY_RUN_RESULTS = frozenset({"", "success"})


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
    # But if the operator hand-edited BACKUP_PATH= back to empty without re-running
    # install, the previously-installed units stay on disk — flag that as a hint
    # to re-run install so the registration matches the new config.
    if not config.get("BACKUP_PATH").strip():
        systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
        stale = [
            str(systemd_dir / name)
            for name in (RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME)
            if (systemd_dir / name).is_file()
        ]
        if stale:
            return [f"systemd units present but BACKUP_PATH is empty; re-run install: {', '.join(stale)}"]
        return []
    failures: list[str] = []
    systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
    for name in (RUN_UNIT_NAME, CHECK_UNIT_NAME, TIMER_UNIT_NAME):
        unit_path = systemd_dir / name
        if not unit_path.is_file():
            failures.append(f"systemd unit missing: {unit_path}; run install")
            continue
        expected = _expected_unit_text(config, name)
        if expected is None:
            failures.append(f"cannot validate {unit_path}: rendering expected unit failed")
            continue
        if unit_path.read_text(encoding="utf-8") != expected:
            failures.append(f"systemd unit out of date: {unit_path}; re-run install")
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
        failures.append(f"timer not enabled: {TIMER_UNIT_NAME} UnitFileState={enabled or 'unknown'}; run install")
    active = _systemctl_value(TIMER_UNIT_NAME, "ActiveState")
    if active != "active":
        failures.append(f"timer not active: {TIMER_UNIT_NAME} ActiveState={active or 'unknown'}")
    last_result = _systemctl_value(RUN_UNIT_NAME, "Result")
    if last_result not in HEALTHY_RUN_RESULTS:
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
    if failures:
        for failure in failures:
            event("error", "doctor failed", reason=failure)
        return 1
    event("info", "doctor passed", vm_count=vm_count, required_kb=required_kb)
    return 0
