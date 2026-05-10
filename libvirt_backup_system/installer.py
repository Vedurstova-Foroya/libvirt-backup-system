from __future__ import annotations

import shlex
import shutil
import stat
import sys
from pathlib import Path

from .backup import backup_root, backup_subpath_is_safe
from .config import Config, default_config_path, prefixed, root_prefix
from .logging_json import event
from .shell import run

UNIT_SERVICE = """[Unit]
Description=Libvirt VM backup orchestrator
Wants=network-online.target
After=network-online.target libvirtd.service
{requires_mounts_for}
[Service]
Type=oneshot
TimeoutStartSec=infinity
EnvironmentFile={config_path}
ExecStart={bin_path} --config {config_path} run
"""


def _render_unit_service(backup_path: str, bin_path: Path, config_path: Path) -> str:
    backup_path = backup_path.strip()
    requires = f"RequiresMountsFor={backup_path}\n" if backup_path else ""
    return UNIT_SERVICE.format(
        requires_mounts_for=requires,
        bin_path=str(bin_path),
        config_path=str(config_path),
    )


UNIT_TIMER = """[Unit]
Description=Run libvirt VM backups on schedule

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


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
    print(
        "\n".join(
            [
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
        ),
        flush=True,
    )


def install(prefix: str | None = None, *, config_path: str | None = None) -> int:
    root = root_prefix(prefix)
    resolved_config = Path(config_path) if config_path else default_config_path(root)
    # First install accepts env overrides so the documented one-shot
    # `BACKUP_PATH=... install` flow works. Once the env file exists it becomes
    # the source of truth; otherwise the systemd unit can desync from it.
    cfg = Config.load(
        config_path=str(resolved_config),
        prefix=str(root),
        apply_env_overrides=not resolved_config.exists(),
    )
    package_src = Path(__file__).resolve().parent
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    package_dst = opt_dir / "libvirt_backup_system"
    bin_path = prefixed("/usr/local/bin/libvirt-backup-system", root)
    systemd_dir = prefixed("/etc/systemd/system", root)

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
    if not cfg.get("BACKUP_PATH").strip():
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
        event(
            "warning",
            "systemd unit installation skipped because BACKUP_PATH is not configured",
            config_path=str(resolved_config),
        )
        _run_systemctl(root, [["systemctl", "daemon-reload"]])
        return 0

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "libvirt-backup-system.service").write_text(
        _render_unit_service(cfg.get("BACKUP_PATH"), bin_path, resolved_config), encoding="utf-8"
    )
    (systemd_dir / "libvirt-backup-system.timer").write_text(
        UNIT_TIMER.format(calendar=cfg.get("SYSTEMD_ON_CALENDAR")), encoding="utf-8"
    )
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


def _resolve_purge_paths(
    root: Path,
    cfg: Config,
    flags: dict[str, bool],
) -> tuple[list[Path], bool]:
    paths: list[Path] = []
    ok = True
    if flags["config"]:
        paths.append(prefixed("/etc/libvirt-backup-system", root))
    if flags["state"]:
        paths.append(prefixed("/var/lib/libvirt-backup-system", root))
    if flags["logs"]:
        paths.append(prefixed("/var/log/libvirt-backup-system", root))
    if flags["backups"]:
        if cfg.get("BACKUP_PATH").strip():
            root_path = backup_root(cfg)
            if backup_subpath_is_safe(cfg, root_path):
                paths.append(root_path)
            else:
                event("error", "backup purge skipped because backup path is unsafe", path=str(root_path))
                ok = False
        else:
            event("warning", "backup purge skipped because BACKUP_PATH is not configured")
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


def uninstall(  # noqa: PLR0913
    prefix: str | None = None,
    *,
    config_path: str | None = None,
    purge_config: bool = False,
    purge_state: bool = False,
    purge_logs: bool = False,
    purge_backups: bool = False,
) -> int:
    root = root_prefix(prefix)
    cfg = Config.load(config_path=config_path, prefix=str(root))
    ok = _run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
        ],
    )
    ok = _remove_installed_files(root) and ok
    purge_paths, purge_ok = _resolve_purge_paths(
        root,
        cfg,
        {"config": purge_config, "state": purge_state, "logs": purge_logs, "backups": purge_backups},
    )
    ok = _purge_paths(purge_paths) and purge_ok and ok
    ok = _run_systemctl(root, [["systemctl", "daemon-reload"]]) and ok
    return 0 if ok else 1
