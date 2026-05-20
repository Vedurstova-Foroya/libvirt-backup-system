from __future__ import annotations

import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from .config import Config, default_config_path, prefixed, root_prefix
from .fish_completion import install_fish_completion
from .installer_uninstall import purge_paths, remove_installed_files, resolve_purge_paths
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .shell import configure_default_timeout
from .systemd_templates import UNIT_SERVICE, UNIT_TIMER
from .systemd_units import (
    render_unit_service,
    render_unit_timer,
    run_systemctl,
    systemctl_available,
    validate_systemd_path,
)

__all__ = ["UNIT_SERVICE", "UNIT_TIMER", "install", "uninstall"]

# Only BACKUP_PATH is honored from the process env on first install: other
# keys render as commented defaults and would desync from the systemd unit.
INSTALL_TIME_ENV_KEYS = ("BACKUP_PATH",)


def _install_time_env_keys_present() -> list[str]:
    return [key for key in INSTALL_TIME_ENV_KEYS if os.environ.get(key) is not None]


def _log_dropped_install_time_env(path: Path) -> None:
    dropped = _install_time_env_keys_present()
    if dropped:
        event(
            "info",
            "existing config kept; install-time environment ignored",
            path=str(path),
            keys=",".join(dropped),
        )


def _install_package(package_src: Path, package_dst: Path) -> None:
    # ``sudo libvirt-backup-system install`` imports this module from
    # /opt/libvirt-backup-system, which is package_dst — an rmtree+copytree
    # there would delete the live source mid-execute. Detect the self-source
    # case and skip the package copy; refresh code from a source checkout via
    # ``sudo python3 -m libvirt_backup_system install``.
    resolved_src = package_src.resolve()
    resolved_dst = package_dst.resolve() if package_dst.exists() else package_dst
    if resolved_src == resolved_dst or resolved_dst in resolved_src.parents:
        event(
            "info",
            "install reusing in-place package (source equals destination); "
            "re-run from a source checkout to refresh code",
            package_src=str(resolved_src),
            package_dst=str(resolved_dst),
        )
        return
    if package_dst.exists():
        shutil.rmtree(package_dst)
    shutil.copytree(package_src, package_dst)


def _write_wrapper(bin_path: Path, root: Path, opt_dir: Path) -> None:
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    python = sys.executable if root != Path("/") else (shutil.which("python3") or "/usr/bin/python3")
    quoted_root = shlex.quote(str(root))
    quoted_opt_dir = shlex.quote(str(opt_dir))
    quoted_python = shlex.quote(python)
    wrapper = (
        "#!/bin/sh\n"
        f"export LIBVIRT_BACKUP_ROOT_PREFIX={quoted_root}\n"
        f"export PYTHONPATH={quoted_opt_dir}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        f'exec {quoted_python} -m libvirt_backup_system "$@"\n'
    )
    bin_path.write_text(wrapper, encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _install_without_backup_path(root: Path, systemd_dir: Path, resolved_config: Path) -> int:
    ok = run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
        ],
    )
    for path in [
        systemd_dir / "libvirt-backup-system.service",
        systemd_dir / "libvirt-backup-system-check.service",
        systemd_dir / "libvirt-backup-system.timer",
    ]:
        try:
            path.unlink()
            event("info", "removed stale systemd unit", path=str(path))
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            event("error", "failed to remove stale systemd unit", path=str(path), error=str(exc))
            ok = False
    event(
        "warning",
        "systemd unit installation skipped because BACKUP_PATH is not configured",
        config_path=str(resolved_config),
    )
    ok = run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    return 0 if ok else 1


def _write_initial_config(path: Path, content: str) -> None:
    # O_CREAT|O_EXCL|0o600 in one open: write_text + chmod 0o600 would leave
    # a window where the env (libvirt URI, possibly secrets) is world-readable.
    # O_EXCL also closes the exists()-vs-open TOCTOU window so a parallel
    # writer's file is left intact instead of truncated.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        _log_dropped_install_time_env(path)
        return
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def _print_install_next_steps(config_path: Path, bin_path: Path) -> None:
    lines = [
        "",
        "Next steps:",
        f"  sudoedit {config_path}",
        "",
        "Set the required backup path, then run start so the systemd mount dependency matches it:",
        "  BACKUP_PATH=/mnt/qnap-backups",
        "",
        "NFS/QNAP mounts are required by default. For an intentionally local backup directory, uncomment:",
        "  BACKUP_REQUIRE_NFS_MOUNT=false",
        "",
        "Retention defaults to keeping 12 months (~1 year) of backups per VM, pruned at the end",
        "of every successful run. Tune BACKUP_RETENTION_MONTHS or set BACKUP_CLEANUP_ON_RUN=false",
        "to disable pruning.",
        "",
        "Then validate and activate the timer:",
        f"  sudo {bin_path} check",
        f"  sudo {bin_path} start",
        f"  sudo {bin_path} doctor",
        "",
    ]
    print("\n".join(lines), flush=True)


def install(prefix: str | None = None, *, config_path: str | None = None) -> int:
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
            return _install_locked(root, resolved_config, cfg)
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


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
    service_text = ""
    check_service_text = ""
    timer_text = ""
    try:
        if backup_path:
            service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="run")
            check_service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="check")
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return 1
    if backup_path:
        rendered_timer = render_unit_timer(root, cfg.get("SYSTEMD_ON_CALENDAR"))
        if rendered_timer is None:
            return 1
        timer_text = rendered_timer

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
    (systemd_dir / "libvirt-backup-system.service").write_text(service_text, encoding="utf-8")
    (systemd_dir / "libvirt-backup-system-check.service").write_text(check_service_text, encoding="utf-8")
    (systemd_dir / "libvirt-backup-system.timer").write_text(timer_text, encoding="utf-8")
    event("info", "installed", opt_dir=str(opt_dir), bin_path=str(bin_path), config_path=str(resolved_config))

    if not systemctl_available(root):
        event("info", "systemd reload skipped", root_prefix=str(root))
        return 0
    return 0 if run_systemctl(root, [["systemctl", "daemon-reload"]]) else 1


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
        ],
    )
    ok = remove_installed_files(root) and ok
    flags = {"config": purge_config, "state": purge_state, "logs": purge_logs}
    ok = purge_paths(resolve_purge_paths(root, cfg, flags)) and ok
    ok = run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    return 0 if ok else 1
