"""``doctor``: superset of ``check`` that also verifies install + repo state.

Kopia migration: in addition to the preflight surface, doctor confirms that
the local kopia repo connects with the configured password, that
``kopia maintenance run --dry-run`` is clean, that ``kopia snapshot verify
--dry-run`` is clean, that no recent QGA quiesce fallback events show up in
the journal, and that every peer repo discoverable under
``BACKUP_PATH/*/kopia-repo/`` is reachable read-only with the shared
password (the "can I cross-host restore" smoke test).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

from . import kopia_client, kopia_repo, kopia_snapshots
from .config import Config, prefixed
from .logging_json import event
from .preflight import collect_check_failures, host_id_drift_failures
from .shell import CommandError, run
from .systemd_units import (
    CHECK_UNIT_NAME,
    KOPIA_FULL_MAINTENANCE_INTERVAL,
    KOPIA_UNIT_DESCRIPTIONS,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
    render_unit_interval_timer,
    render_unit_kopia_service,
    render_unit_service,
    render_unit_timer,
    systemctl_available,
)

WRAPPER_PATH = "/usr/local/bin/libvirt-backup-system"
PACKAGE_PATH = "/opt/libvirt-backup-system/libvirt_backup_system"
SYSTEMD_DIR = "/etc/systemd/system"
HEALTHY_RUN_RESULTS = frozenset({"", "success"})
NEVER_FIRED_PROPERTY_VALUES = frozenset({"", "0"})
DOCTOR_UNIT_NAMES = (
    RUN_UNIT_NAME,
    CHECK_UNIT_NAME,
    TIMER_UNIT_NAME,
    MAINTENANCE_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_FULL_TIMER_NAME,
    VERIFY_UNIT_NAME,
    VERIFY_TIMER_NAME,
)
QUIESCE_FALLBACK_MESSAGE = "QGA quiesce failed; retrying without quiesce (crash-consistent)"


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
        if name == MAINTENANCE_UNIT_NAME:
            return render_unit_kopia_service(bin_path, config.path, kind="maintenance", backup_path=backup_path)
        if name == MAINTENANCE_FULL_UNIT_NAME:
            return render_unit_kopia_service(bin_path, config.path, kind="maintenance-full", backup_path=backup_path)
        if name == VERIFY_UNIT_NAME:
            return render_unit_kopia_service(bin_path, config.path, kind="verify", backup_path=backup_path)
        if name == MAINTENANCE_TIMER_NAME:
            return render_unit_interval_timer(
                description=KOPIA_UNIT_DESCRIPTIONS["maintenance"],
                interval=config.get("KOPIA_MAINTENANCE_INTERVAL"),
            )
        if name == MAINTENANCE_FULL_TIMER_NAME:
            return render_unit_interval_timer(
                description=KOPIA_UNIT_DESCRIPTIONS["maintenance-full"],
                interval=KOPIA_FULL_MAINTENANCE_INTERVAL,
            )
        if name == VERIFY_TIMER_NAME:
            return render_unit_interval_timer(
                description=KOPIA_UNIT_DESCRIPTIONS["verify"],
                interval=config.get("KOPIA_VERIFY_INTERVAL"),
            )
        return render_unit_timer(root, config.get("SYSTEMD_ON_CALENDAR"))
    except ValueError as exc:
        event("error", "doctor cannot render expected unit", unit=name, error=str(exc))
        return None


def _check_units(config: Config) -> list[str]:
    if not config.get("BACKUP_PATH").strip():
        systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
        stale = [str(systemd_dir / name) for name in DOCTOR_UNIT_NAMES if (systemd_dir / name).is_file()]
        if stale:
            return [
                "systemd units present but BACKUP_PATH is empty; run start after fixing config: " + ", ".join(stale)
            ]
        return []
    failures: list[str] = []
    systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
    for name in DOCTOR_UNIT_NAMES:
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
    """Connect to the local repo and probe maintenance dry-run + status."""
    if not config.get("BACKUP_PATH").strip():
        return []
    if not kopia_repo.local_repo_exists(config):
        return [f"local kopia repo missing at {kopia_repo.local_repo_path(config)}; run install"]
    cfg = kopia_repo.local_config_file(config)
    if not cfg.is_file():
        return [f"local kopia config-file missing: {cfg}; run install"]
    try:
        kopia_client.repository_status(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
        )
    except (CommandError, ValueError) as exc:
        return [f"local kopia repo did not connect cleanly: {exc}"]
    return []


def _check_peer_kopia_repos(config: Config) -> list[str]:
    """Read-only connect to every peer repo as a cross-host smoke test."""
    failures: list[str] = []
    for peer in kopia_repo.discover_peer_repos(config):
        if peer.host_id == config.get("HOST_ID"):
            continue
        if kopia_repo.ensure_peer_connected(config, peer.host_id) is None:
            failures.append(f"peer kopia repo {peer.host_id} did not connect; check password sync")
    return failures


def _check_local_kopia_maintenance_dry_run(config: Config) -> list[str]:
    """Confirm ``kopia maintenance run --dry-run`` would succeed.

    Re-uses the same local-repo gate as ``_check_local_kopia_repo`` (no
    duplicate noise when the repo or config file is missing — that already
    surfaces a clearer failure).
    """
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.local_config_file(config)
    if not kopia_repo.local_repo_exists(config) or not cfg.is_file():
        return []
    try:
        kopia_client.maintenance_run(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            dry_run=True,
        )
    except CommandError as exc:
        return [f"local kopia maintenance dry-run failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []


def _check_local_kopia_verify_dry_run(config: Config) -> list[str]:
    """Confirm ``kopia snapshot verify --dry-run`` would succeed."""
    if not config.get("BACKUP_PATH").strip():
        return []
    cfg = kopia_repo.local_config_file(config)
    if not kopia_repo.local_repo_exists(config) or not cfg.is_file():
        return []
    try:
        kopia_snapshots.snapshot_verify(
            config_file=cfg,
            password_file=kopia_repo.password_file_path(config),
            cache_dir=kopia_repo.cache_dir(config),
            dry_run=True,
        )
    except CommandError as exc:
        return [f"local kopia verify dry-run failed: {exc.result.stderr.strip() or exc.result.returncode}"]
    return []


def _check_recent_quiesce_fallbacks(root: Path) -> list[str]:
    """Surface QGA quiesce fallback warnings from the recent journal.

    The vm_snapshot module logs ``warning`` events when libvirt rejects a
    ``--quiesce`` snapshot and the orchestrator retries without it; the
    fallback is crash-consistent rather than application-consistent and
    operators MUST know about it. We scan journalctl output for the warning
    over the last 7 days and surface one informational finding per VM.

    Graceful on cold hosts: missing journalctl or no output is a clean pass
    (the unit may simply never have run yet).
    """
    if not systemctl_available(root):
        return []
    result = run(
        [
            "journalctl",
            "-u",
            RUN_UNIT_NAME,
            "--since",
            "7 days ago",
            "--no-pager",
            "--output=cat",
        ],
        check=False,
    )
    if result.returncode != 0:
        return []
    return _format_quiesce_fallbacks(_collect_quiesce_fallbacks(result.stdout))


def _collect_quiesce_fallbacks(stdout: str) -> dict[str, str]:
    """Return ``{vm: last_seen_ts}`` for each VM logging the fallback."""
    seen: dict[str, str] = {}
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or QUIESCE_FALLBACK_MESSAGE not in stripped:
            continue
        try:
            record_raw: object = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record_raw, dict):
            continue
        # Coerce to dict[str, object] under strict pyright; json keys are
        # always str at runtime but the loader returns dict[Unknown, Unknown].
        record = kopia_client.as_string_keyed(cast("object", record_raw))
        message = record.get("message")
        if message != QUIESCE_FALLBACK_MESSAGE:
            continue
        vm_raw = record.get("vm")
        ts_raw = record.get("ts")
        vm = vm_raw if isinstance(vm_raw, str) and vm_raw else "<unknown-vm>"
        ts = ts_raw if isinstance(ts_raw, str) and ts_raw else ""
        # Last-write-wins: journal entries are ordered, so later iterations
        # overwrite earlier timestamps with the most recent occurrence.
        seen[vm] = ts
    return seen


def _format_quiesce_fallbacks(events: dict[str, str]) -> list[str]:
    findings: list[str] = []
    for vm in sorted(events):
        ts = events[vm]
        suffix = f" (last seen {ts})" if ts else ""
        findings.append(f"recent QGA quiesce fallback for {vm}{suffix}; install qemu-guest-agent inside the VM")
    return findings


def doctor(config: Config) -> int:
    failures, vm_count, required_kb = collect_check_failures(config)
    failures.extend(_check_config_file(config))
    failures.extend(_check_wrapper(config.prefix))
    failures.extend(_check_package(config.prefix))
    failures.extend(_check_units(config))
    failures.extend(_check_runtime_state(config.prefix))
    failures.extend(host_id_drift_failures(config))
    failures.extend(_check_local_kopia_repo(config))
    failures.extend(_check_local_kopia_maintenance_dry_run(config))
    failures.extend(_check_local_kopia_verify_dry_run(config))
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
