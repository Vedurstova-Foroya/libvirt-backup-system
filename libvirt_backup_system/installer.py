from __future__ import annotations

import os
from pathlib import Path

from . import kopia_password, kopia_repo, preflight
from .config import Config, default_config_path, prefixed, root_prefix
from .fish_completion import install_fish_completion
from .installer_binaries import BinaryInstallError, install_kopia, install_nbdcopy
from .installer_helpers import INSTALL_TIME_ENV_KEYS
from .installer_helpers import install_backup_path_configured as _install_backup_path_configured
from .installer_helpers import install_package as _install_package
from .installer_helpers import install_without_backup_path as _install_without_backup_path
from .installer_helpers import log_dropped_install_time_env as _log_dropped_install_time_env
from .installer_helpers import print_install_next_steps as _print_install_next_steps
from .installer_helpers import write_initial_config as _write_initial_config
from .installer_helpers import write_wrapper as _write_wrapper
from .installer_password import install_password as _install_password
from .installer_uninstall import uninstall_locked as _uninstall_locked
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .shell import configure_default_timeout
from .systemd_templates import UNIT_SERVICE, UNIT_TIMER
from .systemd_units import (
    KOPIA_FULL_MAINTENANCE_INTERVAL,
    KOPIA_TIMER_ON_ACTIVE_SEC,
    KOPIA_UNIT_DESCRIPTIONS,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
    render_unit_interval_timer,
    render_unit_kopia_service,
    render_unit_service,
    render_unit_timer,
    run_systemctl,
    systemctl_available,
    validate_systemd_path,
)

__all__ = ["INSTALL_TIME_ENV_KEYS", "UNIT_SERVICE", "UNIT_TIMER", "install", "uninstall"]


def install(
    prefix: str | None = None,
    *,
    config_path: str | None = None,
    password_spec: kopia_password.PasswordSpec | None = None,
) -> int:
    root = root_prefix(prefix)
    try:
        resolved_config = Path(config_path).expanduser() if config_path else default_config_path(root)
        validate_systemd_path(resolved_config, "config_path")
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return 1
    cfg = Config.load(config_path=str(resolved_config), prefix=str(root), apply_env_overrides=False)
    try:
        with acquire_run_lock(cfg):
            if not resolved_config.exists():
                _apply_install_time_env(cfg)
            password_required = _install_backup_path_configured(
                cfg.get("BACKUP_PATH"),
                config_exists=resolved_config.exists(),
            )
            if password_required and _host_id_preflight(cfg) != 0:
                return 1
            if password_required and _repo_preflight(cfg) != 0:
                return 1
            resolved_password_spec = password_spec or kopia_password.PasswordSpec()
            password_supplied = any(
                value is not None
                for value in (
                    resolved_password_spec.literal,
                    resolved_password_spec.file,
                    resolved_password_spec.env_var,
                )
            )
            if password_required or password_supplied:
                password_code = _install_password(cfg, resolved_password_spec)
                if password_code != 0:
                    return password_code
            binary_code = _install_pinned_binaries(root)
            if binary_code != 0:
                return binary_code
            install_code = _install_locked(root, resolved_config, cfg)
            if install_code != 0:
                return install_code
            return _ensure_kopia_repo(cfg) if password_required else 0
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _install_pinned_binaries(root: Path) -> int:
    try:
        install_kopia(prefix=root)
        install_nbdcopy(prefix=root)
    except BinaryInstallError as exc:
        event("error", "pinned binary install failed", error=str(exc))
        return 1
    return 0


def _ensure_kopia_repo(cfg: Config) -> int:
    if not cfg.get("BACKUP_PATH").strip():
        return 0
    if _repo_preflight(cfg) != 0:
        return 1
    return kopia_repo.ensure_local_repo(cfg, apply_global_policy=True)


def _repo_preflight(cfg: Config) -> int:
    failures = preflight.repo_creation_failures(cfg)
    for failure in failures:
        event("error", "kopia repo preflight failed", reason=failure)
    return 1 if failures else 0


def _host_id_preflight(cfg: Config) -> int:
    from . import preflight_host_id

    failure = preflight_host_id.validation_failure(cfg.get("HOST_ID"))
    if failure is None:
        return 0
    event("error", "kopia repo preflight failed", reason=failure)
    return 1


def _apply_install_time_env(cfg: Config) -> None:
    for env_key in INSTALL_TIME_ENV_KEYS:
        env_value = os.environ.get(env_key)
        if env_value is not None:
            cfg.values[env_key] = env_value


def _install_locked(root: Path, resolved_config: Path, cfg: Config) -> int:
    if not resolved_config.exists():
        _apply_install_time_env(cfg)
    try:
        configure_default_timeout(cfg.get("COMMAND_TIMEOUT_SECONDS"))
    except ValueError as exc:
        event("error", "invalid command timeout", error=str(exc))
        return 1
    package_src = Path(__file__).resolve().parent
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    package_dst = opt_dir / "libvirt_backup_system"
    bin_path = prefixed("/usr/local/bin/libvirt-backup-system", root)
    systemd_dir = prefixed("/etc/systemd/system", root)
    backup_path = cfg.get("BACKUP_PATH").strip()
    rendered = _render_units(cfg, root, bin_path, resolved_config) if backup_path else {}
    if backup_path and not rendered:
        return 1

    opt_dir.mkdir(parents=True, exist_ok=True)
    _install_package(package_src, package_dst)
    _write_wrapper(bin_path, root, opt_dir)
    install_fish_completion(root)

    resolved_config.parent.mkdir(parents=True, exist_ok=True)
    if not resolved_config.exists():
        _write_initial_config(resolved_config, cfg.render_env())
    else:
        _log_dropped_install_time_env(resolved_config)

    _print_install_next_steps(resolved_config, bin_path)
    if not backup_path:
        return _install_without_backup_path(root, systemd_dir, resolved_config)

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "libvirt-backup-system.service").write_text(rendered["service"], encoding="utf-8")
    (systemd_dir / "libvirt-backup-system-check.service").write_text(rendered["check"], encoding="utf-8")
    (systemd_dir / "libvirt-backup-system.timer").write_text(rendered["timer"], encoding="utf-8")
    (systemd_dir / MAINTENANCE_UNIT_NAME).write_text(rendered["maintenance_service"], encoding="utf-8")
    (systemd_dir / MAINTENANCE_TIMER_NAME).write_text(rendered["maintenance_timer"], encoding="utf-8")
    (systemd_dir / MAINTENANCE_FULL_UNIT_NAME).write_text(rendered["maintenance_full_service"], encoding="utf-8")
    (systemd_dir / MAINTENANCE_FULL_TIMER_NAME).write_text(rendered["maintenance_full_timer"], encoding="utf-8")
    (systemd_dir / VERIFY_UNIT_NAME).write_text(rendered["verify_service"], encoding="utf-8")
    (systemd_dir / VERIFY_TIMER_NAME).write_text(rendered["verify_timer"], encoding="utf-8")
    event("info", "installed", opt_dir=str(opt_dir), bin_path=str(bin_path), config_path=str(resolved_config))

    if not systemctl_available(root):
        event("info", "systemd reload skipped", root_prefix=str(root))
        return 0
    return 0 if run_systemctl(root, [["systemctl", "daemon-reload"]]) else 1


def _render_units(cfg: Config, root: Path, bin_path: Path, resolved_config: Path) -> dict[str, str]:
    backup_path = cfg.get("BACKUP_PATH").strip()
    try:
        service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="run")
        check_service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="check")
        maintenance_service = render_unit_kopia_service(
            bin_path,
            resolved_config,
            kind="maintenance",
            backup_path=backup_path,
        )
        maintenance_full_service = render_unit_kopia_service(
            bin_path,
            resolved_config,
            kind="maintenance-full",
            backup_path=backup_path,
        )
        verify_service = render_unit_kopia_service(bin_path, resolved_config, kind="verify", backup_path=backup_path)
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return {}
    timer_text = render_unit_timer(root, cfg.get("SYSTEMD_ON_CALENDAR"))
    maintenance_timer = render_unit_interval_timer(
        description=KOPIA_UNIT_DESCRIPTIONS["maintenance"],
        interval=cfg.get("KOPIA_MAINTENANCE_INTERVAL"),
        on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["maintenance"],
    )
    maintenance_full_timer = render_unit_interval_timer(
        description=KOPIA_UNIT_DESCRIPTIONS["maintenance-full"],
        interval=KOPIA_FULL_MAINTENANCE_INTERVAL,
        on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["maintenance-full"],
    )
    verify_timer = render_unit_interval_timer(
        description=KOPIA_UNIT_DESCRIPTIONS["verify"],
        interval=cfg.get("KOPIA_VERIFY_INTERVAL"),
        on_active_sec=KOPIA_TIMER_ON_ACTIVE_SEC["verify"],
    )
    if timer_text is None or maintenance_timer is None or maintenance_full_timer is None or verify_timer is None:
        return {}
    return {
        "service": service_text,
        "check": check_service_text,
        "timer": timer_text,
        "maintenance_service": maintenance_service,
        "maintenance_timer": maintenance_timer,
        "maintenance_full_service": maintenance_full_service,
        "maintenance_full_timer": maintenance_full_timer,
        "verify_service": verify_service,
        "verify_timer": verify_timer,
    }


def uninstall(
    prefix: str | None = None,
    *,
    config_path: str | None = None,
    purge_config: bool = False,
    purge_state: bool = False,
    purge_logs: bool = False,
) -> int:
    root = root_prefix(prefix)
    cfg = Config.load(config_path=config_path, prefix=str(root), apply_env_overrides=False)
    try:
        with acquire_run_lock(cfg):
            return _uninstall_locked(
                root,
                cfg,
                purge_config=purge_config,
                purge_state=purge_state,
                purge_logs=purge_logs,
            )
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1
