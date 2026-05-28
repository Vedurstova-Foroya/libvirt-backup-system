from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import kopia_repo, preflight
from .config import Config, default_config_path, prefixed, root_prefix
from .logging_json import event
from .shell import configure_default_timeout
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
    run_systemctl,
    systemctl_available,
    validate_systemd_path,
)


@dataclass(frozen=True)
class _Rendered:
    config: Config
    resolved_config: Path
    service_text: str
    check_service_text: str
    timer_text: str
    maintenance_service_text: str
    maintenance_timer_text: str
    maintenance_full_service_text: str
    maintenance_full_timer_text: str
    verify_service_text: str
    verify_timer_text: str


def _render_kopia_pair(
    *, bin_path: Path, resolved_config: Path, backup_path: str, kind: str, interval: str
) -> tuple[str, str] | None:
    try:
        service_text = render_unit_kopia_service(
            bin_path,
            resolved_config,
            kind=kind,
            backup_path=backup_path,
        )
    except ValueError as exc:
        event("error", "invalid systemd unit path", error=str(exc))
        return None
    timer_text = render_unit_interval_timer(description=KOPIA_UNIT_DESCRIPTIONS[kind], interval=interval)
    if timer_text is None:
        return None
    return service_text, timer_text


def _render_installed_units(root: Path, config_path: str | None) -> _Rendered | None:
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
    maintenance = _render_kopia_pair(
        bin_path=bin_path,
        resolved_config=resolved_config,
        backup_path=backup_path,
        kind="maintenance",
        interval=cfg.get("KOPIA_MAINTENANCE_INTERVAL"),
    )
    if maintenance is None:
        return None
    maintenance_full = _render_kopia_pair(
        bin_path=bin_path,
        resolved_config=resolved_config,
        backup_path=backup_path,
        kind="maintenance-full",
        interval=KOPIA_FULL_MAINTENANCE_INTERVAL,
    )
    if maintenance_full is None:
        return None
    verify = _render_kopia_pair(
        bin_path=bin_path,
        resolved_config=resolved_config,
        backup_path=backup_path,
        kind="verify",
        interval=cfg.get("KOPIA_VERIFY_INTERVAL"),
    )
    if verify is None:
        return None
    return _Rendered(
        config=cfg,
        resolved_config=resolved_config,
        service_text=service_text,
        check_service_text=check_service_text,
        timer_text=timer_text,
        maintenance_service_text=maintenance[0],
        maintenance_timer_text=maintenance[1],
        maintenance_full_service_text=maintenance_full[0],
        maintenance_full_timer_text=maintenance_full[1],
        verify_service_text=verify[0],
        verify_timer_text=verify[1],
    )


def start(prefix: str | None = None, *, config_path: str | None = None) -> int:
    root = root_prefix(prefix)
    if not systemctl_available(root):
        event("error", "systemctl unavailable; install systemd or run on a systemd host")
        return 1
    rendered = _render_installed_units(root, config_path)
    if rendered is None:
        return 1
    systemd_dir = prefixed("/etc/systemd/system", root)
    systemd_dir.mkdir(parents=True, exist_ok=True)
    (systemd_dir / RUN_UNIT_NAME).write_text(rendered.service_text, encoding="utf-8")
    (systemd_dir / CHECK_UNIT_NAME).write_text(rendered.check_service_text, encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text(rendered.timer_text, encoding="utf-8")
    (systemd_dir / MAINTENANCE_UNIT_NAME).write_text(rendered.maintenance_service_text, encoding="utf-8")
    (systemd_dir / MAINTENANCE_TIMER_NAME).write_text(rendered.maintenance_timer_text, encoding="utf-8")
    (systemd_dir / MAINTENANCE_FULL_UNIT_NAME).write_text(rendered.maintenance_full_service_text, encoding="utf-8")
    (systemd_dir / MAINTENANCE_FULL_TIMER_NAME).write_text(rendered.maintenance_full_timer_text, encoding="utf-8")
    (systemd_dir / VERIFY_UNIT_NAME).write_text(rendered.verify_service_text, encoding="utf-8")
    (systemd_dir / VERIFY_TIMER_NAME).write_text(rendered.verify_timer_text, encoding="utf-8")
    event(
        "info",
        "installed systemd units",
        config_path=str(rendered.resolved_config),
        systemd_dir=str(systemd_dir),
    )
    failures = preflight.repo_creation_failures(rendered.config)
    for failure in failures:
        event("error", "kopia repo preflight failed", reason=failure)
    if failures or kopia_repo.ensure_local_repo(rendered.config, apply_global_policy=True) != 0:
        return 1
    commands: list[list[str]] = [["systemctl", "daemon-reload"]]
    for timer in (TIMER_UNIT_NAME, MAINTENANCE_TIMER_NAME, MAINTENANCE_FULL_TIMER_NAME, VERIFY_TIMER_NAME):
        commands.append(["systemctl", "enable", timer])
        commands.append(["systemctl", "start", timer])
    ok = run_systemctl(root, commands)
    if ok:
        event(
            "info",
            "started systemd timer schedule",
            unit=TIMER_UNIT_NAME,
            maintenance_timer=MAINTENANCE_TIMER_NAME,
            maintenance_full_timer=MAINTENANCE_FULL_TIMER_NAME,
            verify_timer=VERIFY_TIMER_NAME,
        )
    return 0 if ok else 1
