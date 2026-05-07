from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

from .config import Config, default_config_path, prefixed, root_prefix
from .logging_json import event
from .shell import run

UNIT_SERVICE = """[Unit]
Description=Libvirt VM backup orchestrator
Wants=network-online.target
After=network-online.target libvirtd.service

[Service]
Type=oneshot
EnvironmentFile=/etc/libvirt-backup-system/libvirt-backup.env
ExecStart=/usr/local/bin/libvirt-backup-system run
"""


UNIT_TIMER = """[Unit]
Description=Run libvirt VM backups on schedule

[Timer]
OnCalendar={calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""


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
    python = sys.executable if root != Path("/") else "/usr/bin/python3"
    wrapper = (
        "#!/bin/sh\n"
        f"export LIBVIRT_BACKUP_ROOT_PREFIX='{root}'\n"
        f"export PYTHONPATH='{opt_dir}${{PYTHONPATH:+:$PYTHONPATH}}'\n"
        f"exec '{python}' -m libvirt_backup_system \"$@\"\n"
    )
    bin_path.write_text(wrapper, encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(cfg.render_env(), encoding="utf-8")
        config_path.chmod(0o600)

    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / "libvirt-backup-system.service").write_text(UNIT_SERVICE, encoding="utf-8")
    (systemd_dir / "libvirt-backup-system.timer").write_text(
        UNIT_TIMER.format(calendar=cfg.get("SYSTEMD_ON_CALENDAR")), encoding="utf-8"
    )
    event("info", "installed", opt_dir=str(opt_dir), bin_path=str(bin_path), config_path=str(config_path))

    if root == Path("/") and Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        run(["systemctl", "daemon-reload"], check=False)
        run(["systemctl", "enable", "--now", "libvirt-backup-system.timer"], check=False)
    else:
        event("info", "systemd activation skipped", root_prefix=str(root))
    return 0


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

    if root == Path("/") and Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        run(["systemctl", "disable", "--now", "libvirt-backup-system.timer"], check=False)
        run(["systemctl", "stop", "libvirt-backup-system.service"], check=False)

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
        purge_paths.append(cfg.path_value("LOCAL_ROOT"))

    for path in purge_paths:
        if path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            event("info", "purged", path=str(path))

    if root == Path("/") and Path("/run/systemd/system").exists() and shutil.which("systemctl"):
        run(["systemctl", "daemon-reload"], check=False)
    return 0
