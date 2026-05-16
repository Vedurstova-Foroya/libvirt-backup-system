from __future__ import annotations

from pathlib import Path

from .config import Config, default_config_path, prefixed, root_prefix
from .logging_json import event
from .shell import configure_default_timeout
from .systemd_units import (
    CHECK_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    render_unit_service,
    render_unit_timer,
    run_systemctl,
    systemctl_available,
    validate_systemd_path,
)


def _render_installed_units(root: Path, config_path: str | None) -> tuple[Path, str, str, str] | None:
    try:
        resolved_config = Path(config_path).expanduser() if config_path else default_config_path(root)
        validate_systemd_path(resolved_config, "config_path")
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return None
    cfg = Config.load(config_path=str(resolved_config), prefix=str(root), apply_env_overrides=False)
    try:
        configure_default_timeout(cfg.get("COMMAND_TIMEOUT_SECONDS"))
    except ValueError as exc:
        event("error", "invalid command timeout", error=str(exc))
        return None
    backup_path = cfg.get("BACKUP_PATH").strip()
    if not backup_path:
        event(
            "error",
            "BACKUP_PATH is not configured; edit the environment file before start",
            config_path=str(cfg.path),
        )
        return None
    bin_path = prefixed("/usr/local/bin/libvirt-backup-system", root)
    try:
        service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="run")
        check_service_text = render_unit_service(backup_path, bin_path, resolved_config, subcommand="check")
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return None
    timer_text = render_unit_timer(root, cfg.get("SYSTEMD_ON_CALENDAR"))
    if timer_text is None:
        return None
    return resolved_config, service_text, check_service_text, timer_text


def start(prefix: str | None = None, *, config_path: str | None = None) -> int:
    root = root_prefix(prefix)
    if not systemctl_available(root):
        event("error", "systemctl unavailable; install systemd or run on a systemd host")
        return 1
    rendered = _render_installed_units(root, config_path)
    if rendered is None:
        return 1
    resolved_config, service_text, check_service_text, timer_text = rendered
    systemd_dir = prefixed("/etc/systemd/system", root)
    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / RUN_UNIT_NAME).write_text(service_text, encoding="utf-8")
    (systemd_dir / CHECK_UNIT_NAME).write_text(check_service_text, encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text(timer_text, encoding="utf-8")
    event("info", "installed systemd units", config_path=str(resolved_config), systemd_dir=str(systemd_dir))
    ok = run_systemctl(
        root,
        [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", TIMER_UNIT_NAME],
        ],
    )
    if ok:
        event("info", "started systemd timer", unit=TIMER_UNIT_NAME)
    return 0 if ok else 1
