from __future__ import annotations

import shlex
import shutil
import stat
import sys
from pathlib import Path

from .backup import backup_root
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
EnvironmentFile=/etc/libvirt-backup-system/libvirt-backup.env
ExecStart=/usr/local/bin/libvirt-backup-system run
"""


def _render_unit_service(backup_path: str) -> str:
    backup_path = backup_path.strip()
    requires = f"RequiresMountsFor={backup_path}\n" if backup_path else ""
    return UNIT_SERVICE.format(requires_mounts_for=requires)


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
                "Set the required backup path:",
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


def install(prefix: str | None = None) -> int:
    root = root_prefix(prefix)
    cfg = Config.load(prefix=str(root))
    package_src = Path(__file__).resolve().parent
    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    package_dst = opt_dir / "libvirt_backup_system"
    bin_path = prefixed("/usr/local/bin/libvirt-backup-system", root)
    config_path = default_config_path(root)
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

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(cfg.render_env(), encoding="utf-8")
        config_path.chmod(0o600)

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "libvirt-backup-system.service").write_text(
        _render_unit_service(cfg.get("BACKUP_PATH")), encoding="utf-8"
    )
    (systemd_dir / "libvirt-backup-system.timer").write_text(
        UNIT_TIMER.format(calendar=cfg.get("SYSTEMD_ON_CALENDAR")), encoding="utf-8"
    )
    event("info", "installed", opt_dir=str(opt_dir), bin_path=str(bin_path), config_path=str(config_path))
    _print_install_next_steps(config_path, bin_path)

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


def uninstall(
    prefix: str | None = None,
    *,
    purge_config: bool = False,
    purge_state: bool = False,
    purge_logs: bool = False,
    purge_backups: bool = False,
) -> int:
    root = root_prefix(prefix)
    cfg = Config.load(prefix=str(root))

    _run_systemctl(
        root,
        [
            ["systemctl", "disable", "--now", "libvirt-backup-system.timer"],
            ["systemctl", "stop", "libvirt-backup-system.service"],
        ],
    )

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

    opt_dir = prefixed("/opt/libvirt-backup-system", root)
    if opt_dir.exists():
        shutil.rmtree(opt_dir)
        event("info", "removed directory", path=str(opt_dir))

    purge_paths: list[Path] = []
    if purge_config:
        purge_paths.append(prefixed("/etc/libvirt-backup-system", root))
    if purge_state:
        purge_paths.append(prefixed("/var/lib/libvirt-backup-system", root))
    if purge_logs:
        purge_paths.append(prefixed("/var/log/libvirt-backup-system", root))
    if purge_backups:
        if cfg.get("BACKUP_PATH").strip():
            purge_paths.append(backup_root(cfg))
        else:
            event("warning", "backup purge skipped because BACKUP_PATH is not configured")

    for path in purge_paths:
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            event("info", "purged", path=str(path))

    _run_systemctl(root, [["systemctl", "daemon-reload"]])
    return 0
