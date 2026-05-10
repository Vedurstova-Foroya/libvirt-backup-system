from __future__ import annotations

import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from .config import Config, default_config_path, prefixed, root_prefix
from .logging_json import event
from .shell import configure_default_timeout, run
from .systemd_units import UNIT_SERVICE, UNIT_TIMER, render_unit_service, render_unit_timer, validate_systemd_path

__all__ = ["UNIT_SERVICE", "UNIT_TIMER", "install", "uninstall"]

# Only BACKUP_PATH is honored from the process environment during a first
# install. Other keys are intentionally ignored because Config.render_env writes
# them as commented defaults, which silently desyncs the systemd-rendered value
# from what runtime Config.load would later read back from the env file.
INSTALL_TIME_ENV_KEYS = ("BACKUP_PATH",)


def _systemctl_available(root: Path) -> bool:
    return root == Path("/") and Path("/run/systemd/system").exists() and bool(shutil.which("systemctl"))


def _run_systemctl(root: Path, commands: list[list[str]]) -> bool:
    if not _systemctl_available(root):
        return True
    all_ok = True
    for args in commands:
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


def _print_install_next_steps(config_path: Path, bin_path: Path) -> None:
    lines = [
        "",
        "Next steps:",
        f"  sudoedit {config_path}",
        "",
        "Set the required backup path, then re-run install so the systemd mount dependency matches it:",
        "  BACKUP_PATH=/mnt/qnap-backups",
        "",
        "NFS/QNAP mounts are required by default. For an intentionally local backup directory, uncomment:",
        "  BACKUP_REQUIRE_NFS_MOUNT=false",
        "",
        "Then validate and run:",
        f"  sudo {bin_path} check",
        f"  sudo {bin_path} run",
        "",
    ]
    print("\n".join(lines), flush=True)


def install(prefix: str | None = None, *, config_path: str | None = None) -> int:  # noqa: PLR0915
    root = root_prefix(prefix)
    try:
        resolved_config = Path(config_path).expanduser() if config_path else default_config_path(root)
        validate_systemd_path(resolved_config, "config_path")
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return 1
    # Once the env file exists it is the source of truth, so env overrides are
    # off. On a first install we still want the documented `BACKUP_PATH=... install`
    # one-shot to work, but only for the keys listed in INSTALL_TIME_ENV_KEYS —
    # other keys would otherwise be rendered as commented defaults and silently
    # ignored by the systemd unit at runtime.
    cfg = Config.load(
        config_path=str(resolved_config),
        prefix=str(root),
        apply_env_overrides=False,
    )
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
    timer_text = ""
    try:
        if backup_path:
            service_text = render_unit_service(backup_path, bin_path, resolved_config)
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return 1
    if backup_path:
        rendered_timer = render_unit_timer(root, cfg.get("SYSTEMD_ON_CALENDAR"))
        if rendered_timer is None:
            return 1
        timer_text = rendered_timer

    if package_dst.exists():
        shutil.rmtree(package_dst)
    opt_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(package_src, package_dst)

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

    resolved_config.parent.mkdir(parents=True, exist_ok=True)
    if not resolved_config.exists():
        resolved_config.write_text(cfg.render_env(), encoding="utf-8")
        resolved_config.chmod(0o600)

    _print_install_next_steps(resolved_config, bin_path)
    if not backup_path:
        ok = _run_systemctl(
            root,
            [
                ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
                ["systemctl", "stop", "libvirt-backup-system.service"],
            ],
        )
        for path in [
            systemd_dir / "libvirt-backup-system.service",
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
        ok = _run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
        return 0 if ok else 1

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "libvirt-backup-system.service").write_text(service_text, encoding="utf-8")
    (systemd_dir / "libvirt-backup-system.timer").write_text(timer_text, encoding="utf-8")
    event("info", "installed", opt_dir=str(opt_dir), bin_path=str(bin_path), config_path=str(resolved_config))

    if not _systemctl_available(root):
        event("info", "systemd activation skipped", root_prefix=str(root))
        return 0
    return (
        0
        if _run_systemctl(
            root,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", "--now", "libvirt-backup-system.timer"],
            ],
        )
        else 1
    )


def _remove_installed_files(root: Path) -> bool:
    ok = True
    for path in [
        prefixed("/usr/local/bin/libvirt-backup-system", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.service", root),
        prefixed("/etc/systemd/system/libvirt-backup-system.timer", root),
    ]:
        try:
            path.unlink()
            event("info", "removed file", path=str(path))
        except FileNotFoundError:
            pass
        except (PermissionError, OSError) as exc:
            event("error", "failed to remove file", path=str(path), error=str(exc))
            ok = False
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    if opt_dir.exists():
        try:
            shutil.rmtree(opt_dir)
            event("info", "removed directory", path=str(opt_dir))
        except OSError as exc:
            event("error", "failed to remove directory", path=str(opt_dir), error=str(exc))
            ok = False
    return ok


def _resolve_purge_paths(root: Path, cfg: Config, flags: dict[str, bool]) -> tuple[list[Path], bool]:
    paths: list[Path] = []
    ok = True
    if flags["config"]:
        # Remove only the env file; the default parent /etc/libvirt-backup-system
        # may hold sibling files (drop-ins, operator notes) the user dropped in,
        # and a recursive rmtree of the directory would wipe them too.
        paths.append(cfg.path)
    if flags["state"]:
        paths.append(prefixed("/var/lib/libvirt-backup-system", root))
    if flags["logs"]:
        paths.append(prefixed("/var/log/libvirt-backup-system", root))
    return paths, ok


def _purge_paths(paths: list[Path]) -> bool:
    ok = True
    for path in paths:
        if not path.exists():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            event("info", "purged", path=str(path))
        except OSError as exc:
            event("error", "purge failed", path=str(path), error=str(exc))
            ok = False
    return ok


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
        configure_default_timeout(cfg.get("COMMAND_TIMEOUT_SECONDS"))
    except ValueError as exc:
        event("error", "invalid command timeout", error=str(exc))
        return 1
    ok = _run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
        ],
    )
    ok = _remove_installed_files(root) and ok
    flags = {"config": purge_config, "state": purge_state, "logs": purge_logs}
    purge_paths, purge_ok = _resolve_purge_paths(root, cfg, flags)
    ok = _purge_paths(purge_paths) and purge_ok and ok
    ok = _run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    return 0 if ok else 1
