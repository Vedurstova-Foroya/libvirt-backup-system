from __future__ import annotations

import os
from pathlib import Path

from . import kopia_password, kopia_repo, preflight
from .config import Config, default_config_path, prefixed, root_prefix
from .fish_completion import install_fish_completion
from .installer_binaries import BinaryInstallError, install_kopia, install_nbdcopy
from .installer_helpers import INSTALL_TIME_ENV_KEYS
from .installer_helpers import install_package as _install_package
from .installer_helpers import install_without_backup_path as _install_without_backup_path
from .installer_helpers import log_dropped_install_time_env as _log_dropped_install_time_env
from .installer_helpers import print_install_next_steps as _print_install_next_steps
from .installer_helpers import write_initial_config as _write_initial_config
from .installer_helpers import write_wrapper as _write_wrapper
from .installer_password import install_password as _install_password
from .installer_uninstall import purge_paths, remove_installed_files, remove_stale_kopia_units, resolve_purge_paths
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .shell import configure_default_timeout
from .systemd_templates import UNIT_SERVICE, UNIT_TIMER
from .systemd_units import (
    KOPIA_FULL_MAINTENANCE_INTERVAL,
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
    # Env file wins once it exists; first install still honors INSTALL_TIME_ENV_KEYS
    # so the documented `BACKUP_PATH=... install` one-shot works.
    cfg = Config.load(config_path=str(resolved_config), prefix=str(root), apply_env_overrides=False)
    try:
        with acquire_run_lock(cfg):
            # Install the pinned kopia + nbdcopy binaries BEFORE anything
            # else: a fresh host should not need a separate manual
            # ``apt install`` step. Idempotent — the modules skip the
            # download when the right binary is already on disk.
            binary_code = _install_pinned_binaries(root)
            if binary_code != 0:
                return binary_code
            password_code = _install_password(cfg, password_spec or kopia_password.PasswordSpec())
            if password_code != 0:
                return password_code
            install_code = _install_locked(root, resolved_config, cfg)
            if install_code != 0:
                return install_code
            return _ensure_kopia_repo(cfg)
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _install_pinned_binaries(root: Path) -> int:
    """Install pinned kopia + nbdcopy binaries; return 0 on success.

    Network failure or sha256 mismatch is reported as a clean error event
    and a nonzero return so the rest of the install short-circuits before
    touching systemd units. Idempotent: the helpers detect an already-
    present binary at the pinned version and skip the network round-trip.
    """
    try:
        install_kopia(prefix=root)
        install_nbdcopy(prefix=root)
    except BinaryInstallError as exc:
        event("error", "pinned binary install failed", error=str(exc))
        return 1
    return 0


def _ensure_kopia_repo(cfg: Config) -> int:
    """Create or connect the local kopia repo when BACKUP_PATH is configured."""
    if not cfg.get("BACKUP_PATH").strip():
        return 0
    failures = preflight.repo_creation_failures(cfg)
    for failure in failures:
        event("error", "kopia repo preflight failed", reason=failure)
    if failures:
        return 1
    return kopia_repo.ensure_local_repo(cfg, apply_global_policy=True)


def _install_locked(root: Path, resolved_config: Path, cfg: Config) -> int:
    if not resolved_config.exists():
        for env_key in INSTALL_TIME_ENV_KEYS:
            env_value = os.environ.get(env_key)
            if env_value is not None:
                cfg.values[env_key] = env_value
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
        # Scrub kopia housekeeping units before the legacy backup pair cleanup.
        if not remove_stale_kopia_units(systemd_dir):
            return 1
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
    """Render every unit file the installer writes. Empty dict signals failure."""
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
    )
    maintenance_full_timer = render_unit_interval_timer(
        description=KOPIA_UNIT_DESCRIPTIONS["maintenance-full"],
        interval=KOPIA_FULL_MAINTENANCE_INTERVAL,
    )
    verify_timer = render_unit_interval_timer(
        description=KOPIA_UNIT_DESCRIPTIONS["verify"],
        interval=cfg.get("KOPIA_VERIFY_INTERVAL"),
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


def _uninstall_locked(
    root: Path,
    cfg: Config,
    *,
    purge_config: bool,
    purge_state: bool,
    purge_logs: bool,
) -> int:
    # A broken COMMAND_TIMEOUT_SECONDS must not abort the very uninstall
    # (especially `--purge-config`) meant to clean it up — log and continue.
    try:
        configure_default_timeout(cfg.get("COMMAND_TIMEOUT_SECONDS"))
    except ValueError as exc:
        event("warning", "invalid command timeout; uninstall continuing with default", error=str(exc))
    ok = run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
            ["systemctl", "disable", "--now", MAINTENANCE_TIMER_NAME],
            ["systemctl", "stop", MAINTENANCE_UNIT_NAME],
            ["systemctl", "disable", "--now", MAINTENANCE_FULL_TIMER_NAME],
            ["systemctl", "stop", MAINTENANCE_FULL_UNIT_NAME],
            ["systemctl", "disable", "--now", VERIFY_TIMER_NAME],
            ["systemctl", "stop", VERIFY_UNIT_NAME],
        ],
    )
    ok = remove_installed_files(root) and ok
    flags = {"config": purge_config, "state": purge_state, "logs": purge_logs}
    ok = purge_paths(resolve_purge_paths(root, cfg, flags)) and ok
    ok = run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    _print_kopia_repo_retention_hint(cfg)
    return 0 if ok else 1


def _print_kopia_repo_retention_hint(cfg: Config) -> None:
    """Tell the operator we kept the local kopia repo and how to delete it.

    Uninstall removes systemd units only; the local kopia repo and the
    password file are operator decisions. We surface the on-disk path so an
    operator who *does* want to wipe backups can do so in one ``rm``.
    """
    backup_path = cfg.get("BACKUP_PATH").strip()
    if not backup_path:
        return
    try:
        repo_path = kopia_repo.local_repo_path(cfg)
    except ValueError:
        return
    event(
        "info",
        "kopia repo retained",
        path=str(repo_path),
        hint=f"rm -rf {repo_path} to delete backups",
    )
