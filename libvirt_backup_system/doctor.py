"""``doctor``: superset of ``check`` that also verifies install + repo state.

Kopia migration: in addition to the preflight surface, doctor confirms that
the local kopia repo connects with the configured password, that
``kopia maintenance run --safety=none`` is clean, that a lightweight
``kopia snapshot verify --verify-files-percent=0`` is clean, that no recent
QGA quiesce fallback events show up in the journal, and that every peer repo
discoverable under
``BACKUP_PATH/*/kopia-repo/`` is reachable read-only with the shared
password (the "can I cross-host restore" smoke test).
"""

from __future__ import annotations

from pathlib import Path

from . import doctor_kopia, doctor_quiesce, doctor_units
from .config import Config
from .logging_json import event
from .preflight import collect_check_failures, host_id_drift_failures
from .shell import run
from .systemd_units import (
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_TIMER_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    render_unit_interval_timer,
    render_unit_kopia_service,
    render_unit_service,
    render_unit_timer,
    systemctl_available,
)

WRAPPER_PATH = doctor_units.WRAPPER_PATH
PACKAGE_PATH = doctor_units.PACKAGE_PATH
SYSTEMD_DIR = doctor_units.SYSTEMD_DIR
HEALTHY_RUN_RESULTS = frozenset({"", "success"})
NEVER_FIRED_PROPERTY_VALUES = frozenset({"", "0"})
DOCTOR_UNIT_NAMES = doctor_units.DOCTOR_UNIT_NAMES
QUIESCE_FALLBACK_MESSAGE = doctor_quiesce.QUIESCE_FALLBACK_MESSAGE
SCHEDULE_TIMER_NAMES = (
    TIMER_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_FULL_TIMER_NAME,
    VERIFY_TIMER_NAME,
)


def _check_wrapper(root: Path) -> list[str]:
    return doctor_units.check_wrapper(root)


def _check_package(root: Path) -> list[str]:
    return doctor_units.check_package(root)


def _check_config_file(config: Config) -> list[str]:
    return doctor_units.check_config_file(config)


def _expected_unit_text(config: Config, name: str) -> str | None:
    return doctor_units.expected_unit_text(
        config,
        name,
        render_unit_service=render_unit_service,
        render_unit_kopia_service=render_unit_kopia_service,
        render_unit_interval_timer=render_unit_interval_timer,
        render_unit_timer=render_unit_timer,
    )


def _check_units(config: Config) -> list[str]:
    return doctor_units.check_units(config, unit_names=DOCTOR_UNIT_NAMES, expected_unit_text=_expected_unit_text)


def _systemctl_value(unit: str, prop: str) -> str:
    result = run(["systemctl", "show", unit, f"--property={prop}", "--value"], check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _check_runtime_state(root: Path) -> list[str]:
    if not systemctl_available(root):
        return []
    failures: list[str] = []
    for timer in SCHEDULE_TIMER_NAMES:
        enabled = _systemctl_value(timer, "UnitFileState")
        if enabled != "enabled":
            failures.append(f"timer not enabled: {timer} UnitFileState={enabled or 'unknown'}; run start")
        active = _systemctl_value(timer, "ActiveState")
        if active != "active":
            failures.append(f"timer not active: {timer} ActiveState={active or 'unknown'}")
    if _systemctl_value(RUN_UNIT_NAME, "NeedDaemonReload") == "yes":
        failures.append(f"systemd needs daemon-reload: {RUN_UNIT_NAME} cached unit is stale; run start")
    last_result = _systemctl_value(RUN_UNIT_NAME, "Result")
    last_trigger = _systemctl_value(TIMER_UNIT_NAME, "LastTriggerUSec")
    next_elapse = _systemctl_value(TIMER_UNIT_NAME, "NextElapseUSecRealtime")
    if last_trigger in NEVER_FIRED_PROPERTY_VALUES:
        if next_elapse in NEVER_FIRED_PROPERTY_VALUES:
            failures.append(
                f"timer has not fired and no next elapse scheduled: {TIMER_UNIT_NAME}; check systemctl list-timers"
            )
    elif last_result not in HEALTHY_RUN_RESULTS:
        failures.append(f"last run failed: {RUN_UNIT_NAME} Result={last_result}")
    return failures


def _check_local_kopia_repo(config: Config) -> list[str]:
    return doctor_kopia.check_local_kopia_repo(config)


def _check_peer_kopia_repos(config: Config) -> list[str]:
    return doctor_kopia.check_peer_kopia_repos(config)


def _check_local_kopia_maintenance_probe(config: Config) -> list[str]:
    return doctor_kopia.check_local_kopia_maintenance_probe(config)


def _check_local_kopia_verify_probe(config: Config) -> list[str]:
    return doctor_kopia.check_local_kopia_verify_probe(config)


def _check_recent_quiesce_fallbacks(root: Path) -> list[str]:
    return doctor_quiesce.check_recent_quiesce_fallbacks(
        root,
        systemctl_available=systemctl_available,
        run_command=run,
    )


def _collect_quiesce_fallbacks(stdout: str) -> dict[str, str]:
    return doctor_quiesce.collect_quiesce_fallbacks(stdout)


def _format_quiesce_fallbacks(events: dict[str, str]) -> list[str]:
    return doctor_quiesce.format_quiesce_fallbacks(events)


def doctor(config: Config) -> int:
    failures, vm_count, required_kb = collect_check_failures(config)
    failures.extend(_check_config_file(config))
    failures.extend(_check_wrapper(config.prefix))
    failures.extend(_check_package(config.prefix))
    failures.extend(_check_units(config))
    failures.extend(_check_runtime_state(config.prefix))
    failures.extend(host_id_drift_failures(config))
    failures.extend(_check_local_kopia_repo(config))
    failures.extend(_check_local_kopia_maintenance_probe(config))
    failures.extend(_check_local_kopia_verify_probe(config))
    failures.extend(_check_peer_kopia_repos(config))
    advisories = _check_recent_quiesce_fallbacks(config.prefix)
    if failures:
        for failure in failures:
            event("error", "doctor failed", reason=failure)
        return 1
    for advisory in advisories:
        event("warning", "doctor advisory", reason=advisory)
    event("info", "doctor passed", vm_count=vm_count, required_kb=required_kb)
    return 0
