"""Tests for systemd_start and systemd_render covering render paths,
interval timer validation, and systemd_units thin wrappers.

Split from ``test_systemd_start.py`` to stay within the 300-line limit.
"""

from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_start import start


def _config_text(backup_dir: Path, *extra: str) -> str:
    return f"BACKUP_PATH={backup_dir}\nBACKUP_REQUIRE_NFS_MOUNT=false\nHOST_ID=host-a\n" + "".join(extra)


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
    """systemd_units.py line 119: thin wrapper quote_systemd_path."""
    from libvirt_backup_system.systemd_units import quote_systemd_path

    result = quote_systemd_path("/some/path")
    assert result == '"/some/path"'


def test_systemd_units_escape_systemd_path_wrapper() -> None:
    """systemd_units.py line 123: thin wrapper escape_systemd_path."""
    from libvirt_backup_system.systemd_units import escape_systemd_path

    result = escape_systemd_path("/some/path with space")
    assert result == "/some/path\\ with\\ space"


def test_systemd_units_requires_mounts_for_wrapper() -> None:
    """systemd_units.py line 127: thin wrapper requires_mounts_for."""
    from libvirt_backup_system.systemd_units import requires_mounts_for

    result = requires_mounts_for("/mnt/backup")
    assert "RequiresMountsFor=/mnt/backup" in result


def test_systemd_units_has_control_char_wrapper() -> None:
    """systemd_units.py line 135: thin wrapper has_control_char."""
    from libvirt_backup_system.systemd_units import has_control_char

    assert has_control_char("hello\x01world") is True
    assert has_control_char("hello world") is False
