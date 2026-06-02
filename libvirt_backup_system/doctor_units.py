from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .config import Config, prefixed
from .logging_json import event
from .systemd_units import (
    CHECK_UNIT_NAME,
    KOPIA_FULL_MAINTENANCE_INTERVAL,
    KOPIA_TIMER_ON_ACTIVE_SEC,
    KOPIA_UNIT_DESCRIPTIONS,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)

WRAPPER_PATH = "/usr/local/bin/libvirt-backup-system"
PACKAGE_PATH = "/opt/libvirt-backup-system/libvirt_backup_system"
SYSTEMD_DIR = "/etc/systemd/system"
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


class RenderUnitService(Protocol):
    def __call__(  # pragma: no branch
        self, backup_path: str, bin_path: Path, config_path: Path, *, subcommand: str = "run"
    ) -> str: ...


class RenderUnitKopiaService(Protocol):
    def __call__(  # pragma: no branch
        self, bin_path: Path, config_path: Path, *, kind: str, backup_path: str = ""
    ) -> str: ...


RenderUnitTimer = Callable[[Path, str], str | None]
RenderIntervalTimer = Callable[..., str | None]
ExpectedUnitText = Callable[[Config, str], str | None]


def check_wrapper(root: Path) -> list[str]:
    bin_path = prefixed(WRAPPER_PATH, root)
    if not bin_path.is_file():
        return [f"wrapper script missing: {bin_path}; run install"]
    if not os.access(bin_path, os.X_OK):
        return [f"wrapper script not executable: {bin_path}; re-run install"]
    return []


def check_package(root: Path) -> list[str]:
    package_dst = prefixed(PACKAGE_PATH, root)
    if not package_dst.is_dir():
        return [f"package directory missing: {package_dst}; run install"]
    return []


def check_config_file(config: Config) -> list[str]:
    if not config.path.is_file():
        return [f"config file missing: {config.path}; run install"]
    return []


def expected_unit_text(
    config: Config,
    name: str,
    *,
    render_unit_service: RenderUnitService,
    render_unit_kopia_service: RenderUnitKopiaService,
    render_unit_interval_timer: RenderIntervalTimer,
    render_unit_timer: RenderUnitTimer,
) -> str | None:
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
                on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["maintenance"],
            )
        if name == MAINTENANCE_FULL_TIMER_NAME:
            return render_unit_interval_timer(
                description=KOPIA_UNIT_DESCRIPTIONS["maintenance-full"],
                interval=KOPIA_FULL_MAINTENANCE_INTERVAL,
                on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["maintenance-full"],
            )
        if name == VERIFY_TIMER_NAME:
            return render_unit_interval_timer(
                description=KOPIA_UNIT_DESCRIPTIONS["verify"],
                interval=config.get("KOPIA_VERIFY_INTERVAL"),
                on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["verify"],
            )
        return render_unit_timer(root, config.get("SYSTEMD_ON_CALENDAR"))
    except ValueError as exc:
        event("error", "doctor cannot render expected unit", unit=name, error=str(exc))
        return None


def check_units(config: Config, *, unit_names: Sequence[str], expected_unit_text: ExpectedUnitText) -> list[str]:
    systemd_dir = prefixed(SYSTEMD_DIR, config.prefix)
    if not config.get("BACKUP_PATH").strip():
        stale = [str(systemd_dir / name) for name in unit_names if (systemd_dir / name).is_file()]
        if stale:
            return [
                "systemd units present but BACKUP_PATH is empty; run start after fixing config: " + ", ".join(stale)
            ]
        return []
    failures: list[str] = []
    for name in unit_names:
        unit_path = systemd_dir / name
        if not unit_path.is_file():
            failures.append(f"systemd unit missing: {unit_path}; run start")
            continue
        expected = expected_unit_text(config, name)
        if expected is None:
            failures.append(f"cannot validate {unit_path}: rendering expected unit failed")
            continue
        if unit_path.read_text(encoding="utf-8") != expected:
            failures.append(f"systemd unit out of date: {unit_path}; run start")
    return failures
