from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_start import start
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    KOPIA_TIMER_ON_ACTIVE_SEC,
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)


def _config_text(backup_dir: Path, *extra: str) -> str:
    return f"BACKUP_PATH={backup_dir}\nBACKUP_REQUIRE_NFS_MOUNT=false\nHOST_ID=host-a\n" + "".join(extra)


def test_start_returns_one_when_systemctl_unavailable(tmp_path: Path, capsys) -> None:
    assert start(str(tmp_path)) == 1
    assert "systemctl unavailable" in capsys.readouterr().err


def test_start_requires_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "BACKUP_PATH is not configured" in err


def test_start_rejects_relative_config_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)

    assert start(str(tmp_path), config_path="relative.env") == 1

    err = capsys.readouterr().err
    assert "config_path must be an absolute path" in err


def test_start_rejects_relative_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config.parent.mkdir(parents=True)
    config.write_text("BACKUP_PATH=relative/backups\n", encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "BACKUP_PATH must be an absolute path" in err


def test_render_unit_kopia_service_unknown_kind_raises(tmp_path: Path) -> None:
    from libvirt_backup_system.systemd_units import render_unit_kopia_service

    with pytest.raises(ValueError, match="unknown kopia unit kind"):
        render_unit_kopia_service(
            tmp_path / "usr/local/bin/lbs",
            tmp_path / "etc/cfg",
            kind="not-a-real-kind",
        )


def test_render_unit_kopia_service_includes_backup_mount(tmp_path: Path) -> None:
    from libvirt_backup_system.systemd_units import render_unit_kopia_service

    backup_dir = tmp_path / "backups with space"
    text = render_unit_kopia_service(
        tmp_path / "usr/local/bin/lbs",
        tmp_path / "etc/cfg",
        kind="maintenance",
        backup_path=str(backup_dir),
    )
    escaped_backup_dir = str(backup_dir).replace(" ", "\\ ")
    assert f"RequiresMountsFor={escaped_backup_dir}" in text


def test_render_unit_interval_timer_rejects_control_char(capsys) -> None:
    from libvirt_backup_system.systemd_units import render_unit_interval_timer

    assert render_unit_interval_timer(description="x", interval="24h\nbad") is None
    err = capsys.readouterr().err
    assert "timer interval must not contain control characters" in err


def test_start_rejects_invalid_kopia_service_path_via_render_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # systemd_start._render_kopia_pair MUST surface a ValueError from
    # render_unit_kopia_service. Patch the imported renderer to simulate
    # the failure path production install would hit on a hostile config_path.
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir), encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise ValueError("boom rendering kopia service")

    monkeypatch.setattr("libvirt_backup_system.systemd_start.render_unit_kopia_service", boom)

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "boom rendering kopia service" in err


def test_start_rejects_invalid_maintenance_interval(tmp_path: Path, monkeypatch, capsys) -> None:
    # An empty KOPIA_MAINTENANCE_INTERVAL must short-circuit start instead of
    # rendering a malformed [Timer] body that systemd would reject on
    # daemon-reload.
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "KOPIA_MAINTENANCE_INTERVAL= \n"), encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "timer interval must not be empty" in err


def test_start_rejects_invalid_verify_interval(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "KOPIA_VERIFY_INTERVAL=--bad\n"), encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "timer interval must not start with '-'" in err


def test_start_rejects_invalid_kopia_service_path(tmp_path: Path, monkeypatch, capsys) -> None:
    # render_unit_kopia_service inherits validate_systemd_path; trigger that
    # branch by handing it a config_path with a backtick (forbidden char).
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)

    assert start(str(tmp_path), config_path=str(tmp_path / "bad`name.env")) == 1

    err = capsys.readouterr().err
    assert "config_path must not contain '`'" in err


def test_start_rejects_invalid_timer_calendar(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "SYSTEMD_ON_CALENDAR=--help\n"), encoding="utf-8")

    assert start(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "SYSTEMD_ON_CALENDAR must not start with '-'" in err


def test_start_installs_units_enables_and_starts_timer_schedule(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir), encoding="utf-8")
    calls: list[list[str]] = []
    order: list[str] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        order.append("systemctl")
        calls.append(args)
        return CommandResult(args, 0, "", "")

    def fake_ensure(cfg: object, *, apply_global_policy: bool = True) -> int:
        order.append("ensure")
        assert apply_global_policy is True
        return 0

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo", fake_ensure)

    assert start(str(tmp_path)) == 0

    assert calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", TIMER_UNIT_NAME],
        ["systemctl", "start", TIMER_UNIT_NAME],
        ["systemctl", "enable", MAINTENANCE_TIMER_NAME],
        ["systemctl", "start", MAINTENANCE_TIMER_NAME],
        ["systemctl", "enable", MAINTENANCE_FULL_TIMER_NAME],
        ["systemctl", "start", MAINTENANCE_FULL_TIMER_NAME],
        ["systemctl", "enable", VERIFY_TIMER_NAME],
        ["systemctl", "start", VERIFY_TIMER_NAME],
    ]
    assert order[0] == "ensure"
    systemd_dir = tmp_path / "etc/systemd/system"
    assert (systemd_dir / RUN_UNIT_NAME).exists()
    assert (systemd_dir / CHECK_UNIT_NAME).exists()
    assert (systemd_dir / TIMER_UNIT_NAME).exists()
    assert (systemd_dir / MAINTENANCE_UNIT_NAME).exists()
    assert (systemd_dir / MAINTENANCE_TIMER_NAME).exists()
    assert (systemd_dir / MAINTENANCE_FULL_UNIT_NAME).exists()
    assert (systemd_dir / MAINTENANCE_FULL_TIMER_NAME).exists()
    assert (systemd_dir / VERIFY_UNIT_NAME).exists()
    assert (systemd_dir / VERIFY_TIMER_NAME).exists()
    out = capsys.readouterr().out
    assert "installed systemd units" in out
    assert "started systemd timer schedule" in out
    assert "Persistent=true" not in (systemd_dir / TIMER_UNIT_NAME).read_text(encoding="utf-8")
    maintenance_timer_text = (systemd_dir / MAINTENANCE_TIMER_NAME).read_text(encoding="utf-8")
    maintenance_full_timer_text = (systemd_dir / MAINTENANCE_FULL_TIMER_NAME).read_text(encoding="utf-8")
    verify_timer_text = (systemd_dir / VERIFY_TIMER_NAME).read_text(encoding="utf-8")
    assert f"OnActiveSec={KOPIA_TIMER_ON_ACTIVE_SEC['maintenance']}" in maintenance_timer_text
    assert "OnBootSec" not in maintenance_timer_text
    assert "OnUnitActiveSec=24h" in maintenance_timer_text
    assert f"OnActiveSec={KOPIA_TIMER_ON_ACTIVE_SEC['maintenance-full']}" in maintenance_full_timer_text
    assert "OnBootSec" not in maintenance_full_timer_text
    assert f"OnActiveSec={KOPIA_TIMER_ON_ACTIVE_SEC['verify']}" in verify_timer_text
    assert "OnBootSec" not in verify_timer_text
    assert "OnUnitActiveSec=7d" in maintenance_full_timer_text
    assert "--full" in (systemd_dir / MAINTENANCE_FULL_UNIT_NAME).read_text(encoding="utf-8")
    assert f"RequiresMountsFor={backup_dir}" in (systemd_dir / MAINTENANCE_UNIT_NAME).read_text(encoding="utf-8")
    assert f"RequiresMountsFor={backup_dir}" in (systemd_dir / VERIFY_UNIT_NAME).read_text(encoding="utf-8")
    assert "OnUnitActiveSec=7d" in verify_timer_text


def test_start_configures_timeout_before_calendar_validation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "COMMAND_TIMEOUT_SECONDS=7\n"), encoding="utf-8")
    configured: list[str] = []

    def fake_configure(value: str) -> None:
        configured.append(value)

    def fake_render_timer(root: Path, calendar: str) -> str:
        assert configured == ["7"]
        return "[Timer]\n"

    monkeypatch.setattr("libvirt_backup_system.systemd_start.configure_default_timeout", fake_configure)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.render_unit_timer", fake_render_timer)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo", lambda *_a, **_k: 0)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.run_systemctl", lambda root, commands: True)

    assert start(str(tmp_path)) == 0


def test_start_rejects_invalid_command_timeout(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "COMMAND_TIMEOUT_SECONDS=0\n"), encoding="utf-8")

    assert start(str(tmp_path)) == 1
    assert "command timeout must be greater than 0" in capsys.readouterr().err


def test_start_returns_one_when_systemctl_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir), encoding="utf-8")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 1, "", "boom")

    monkeypatch.setattr("libvirt_backup_system.systemd_units.run", fake_run)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo", lambda *_a, **_k: 0)

    assert start(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "systemctl daemon-reload failed" in err
    assert "systemctl enable libvirt-backup-system.timer failed" in err
    assert "systemctl start libvirt-backup-system.timer failed" in err
    assert "systemctl enable libvirt-backup-system-maintenance.timer failed" in err
    assert "systemctl enable libvirt-backup-system-maintenance-full.timer failed" in err
    assert "systemctl enable libvirt-backup-system-verify.timer failed" in err


def test_start_returns_one_when_timer_text_is_none(tmp_path: Path, monkeypatch, capsys) -> None:
    """Line 119 in systemd_start: _render_installed_units returns None when
    the maintenance-full kopia pair fails to render (timer_text is None)."""
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    # Valid maintenance interval so the first _render_kopia_pair succeeds,
    # but patch render_unit_interval_timer to fail only for maintenance-full.
    config.write_text(_config_text(backup_dir), encoding="utf-8")
    original_render = __import__(
        "libvirt_backup_system.systemd_render", fromlist=["render_unit_interval_timer"]
    ).render_unit_interval_timer
    call_count = 0

    def selective_fail(*, description: str, interval: str, on_active_sec: str = "15min") -> str | None:
        nonlocal call_count
        call_count += 1
        # First call is for "maintenance", let it succeed.
        # Second call is for "maintenance-full", force it to fail.
        if call_count == 2:
            return None
        return original_render(description=description, interval=interval, on_active_sec=on_active_sec)

    monkeypatch.setattr("libvirt_backup_system.systemd_start.render_unit_interval_timer", selective_fail)

    assert start(str(tmp_path)) == 1


def test_requires_mounts_for_empty_backup_path() -> None:
    """systemd_render.py line 50: empty backup_path after strip returns ''."""
    from libvirt_backup_system.systemd_render import requires_mounts_for

    assert requires_mounts_for("") == ""
    assert requires_mounts_for("   ") == ""


def test_render_unit_interval_timer_rejects_empty_on_active_sec(capsys) -> None:
    """systemd_render.py line 134-135: empty on_active_sec."""
    from libvirt_backup_system.systemd_render import render_unit_interval_timer

    assert render_unit_interval_timer(description="x", interval="24h", on_active_sec="  ") is None
    err = capsys.readouterr().err
    assert "timer OnActiveSec must not be empty" in err


def test_render_unit_interval_timer_rejects_control_char_on_active_sec(capsys) -> None:
    """systemd_render.py line 137-138: control char in on_active_sec."""
    from libvirt_backup_system.systemd_render import render_unit_interval_timer

    assert render_unit_interval_timer(description="x", interval="24h", on_active_sec="15min\x01") is None
    err = capsys.readouterr().err
    assert "timer OnActiveSec must not contain control characters" in err


def test_render_unit_interval_timer_rejects_dash_on_active_sec(capsys) -> None:
    """systemd_render.py line 140-141: on_active_sec starts with '-'."""
    from libvirt_backup_system.systemd_render import render_unit_interval_timer

    assert render_unit_interval_timer(description="x", interval="24h", on_active_sec="-15min") is None
    err = capsys.readouterr().err
    assert "timer OnActiveSec must not start with '-'" in err


def test_systemd_units_quote_systemd_path_wrapper() -> None:
    """systemd_units.py line 119: thin wrapper _quote_systemd_path."""
    from libvirt_backup_system.systemd_units import _quote_systemd_path

    result = _quote_systemd_path("/some/path")
    assert result == '"/some/path"'


def test_systemd_units_escape_systemd_path_wrapper() -> None:
    """systemd_units.py line 123: thin wrapper _escape_systemd_path."""
    from libvirt_backup_system.systemd_units import _escape_systemd_path

    result = _escape_systemd_path("/some/path with space")
    assert result == "/some/path\\ with\\ space"


def test_systemd_units_requires_mounts_for_wrapper() -> None:
    """systemd_units.py line 127: thin wrapper _requires_mounts_for."""
    from libvirt_backup_system.systemd_units import _requires_mounts_for

    result = _requires_mounts_for("/mnt/backup")
    assert "RequiresMountsFor=/mnt/backup" in result


def test_systemd_units_has_control_char_wrapper() -> None:
    """systemd_units.py line 135: thin wrapper _has_control_char."""
    from libvirt_backup_system.systemd_units import _has_control_char

    assert _has_control_char("hello\x01world") is True
    assert _has_control_char("hello world") is False
