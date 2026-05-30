from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.installer import install
from tests.unit.conftest import write_kopia_password_file


def test_install_returns_password_failure_code(tmp_path: Path, monkeypatch, capsys) -> None:
    # Pre-existing password file with a different value than the spec must
    # short-circuit ``install`` before the systemd / package work runs, so a
    # subsequent re-install never silently rotates the master key.
    from libvirt_backup_system.kopia_password import PasswordSpec

    write_kopia_password_file(tmp_path, value="existing")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    assert install(str(tmp_path), password_spec=PasswordSpec(literal="different")) == 1
    err = capsys.readouterr().err
    assert "different value" in err
    # Systemd units must not have been written when the password step fails.
    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.service").exists()


def test_install_reinstall_reports_insecure_password_file_cleanly(tmp_path: Path, monkeypatch, capsys) -> None:
    password = write_kopia_password_file(tmp_path, value="existing")
    password.chmod(0o644)
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(backup_path))

    assert install(str(tmp_path)) == 1

    err = capsys.readouterr().err
    assert "kopia password file security failure" in err
    assert "must be mode 600" in err


def test_install_rejects_relative_config_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert install(str(tmp_path), config_path="relative.env") == 1

    assert not (tmp_path / "relative.env").exists()
    err = capsys.readouterr().err
    assert "config_path must be an absolute path for systemd units" in err


def test_install_rejects_control_char_config_path(tmp_path: Path, capsys) -> None:
    assert install(str(tmp_path), config_path=str(tmp_path / "bad\nname.env")) == 1

    err = capsys.readouterr().err
    assert "config_path must not contain control characters for systemd units" in err


def test_install_creates_config_with_mode_0o600(tmp_path: Path, monkeypatch) -> None:
    # Env file may grow secrets via operator edits; install must atomically
    # create it with mode 0o600 instead of write_text+chmod (world-readable window).
    import stat as _stat

    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    assert install(str(tmp_path)) == 0
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    assert _stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_write_initial_config_skips_write_when_file_appears_under_race(tmp_path: Path, monkeypatch) -> None:
    # A parallel writer between exists() and our O_EXCL open must not be
    # truncated; FileExistsError is silently ignored.
    from libvirt_backup_system.installer import _write_initial_config

    config_path = tmp_path / "config.env"
    config_path.write_text("pre-existing\n", encoding="utf-8")
    real_open = os.open

    def fake_open(path, flags, mode=0o777, *, dir_fd=None):
        del dir_fd
        if str(path) == str(config_path):
            raise FileExistsError(17, "file exists", str(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr("libvirt_backup_system.installer_helpers.os.open", fake_open)
    _write_initial_config(config_path, "new-content\n")
    assert config_path.read_text(encoding="utf-8") == "pre-existing\n"


def test_install_rejects_backticked_config_path(tmp_path: Path, capsys) -> None:
    # Backticks survive _quote_systemd_path: systemd does not run /bin/sh, but
    # operator tooling re-rendering the unit through a shell would expand them.
    assert install(str(tmp_path), config_path=str(tmp_path / "bad`name.env")) == 1
    err = capsys.readouterr().err
    assert "config_path must not contain '`'" in err


def test_install_rejects_relative_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BACKUP_PATH", "relative/backups")
    write_kopia_password_file(tmp_path)

    assert install(str(tmp_path)) == 1

    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.service").exists()
    err = capsys.readouterr().err
    assert "BACKUP_PATH must be an absolute path for systemd units" in err


def test_install_reports_stale_systemd_unit_removal_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    service_path.parent.mkdir(parents=True)
    service_path.write_text("stale\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == service_path:
            raise PermissionError("no perms")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert install(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "failed to remove stale systemd unit" in err
    assert "no perms" in err


def test_install_reports_stale_kopia_unit_removal_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # Re-install with BACKUP_PATH unset must scrub leftover maintenance +
    # verify unit files; a PermissionError on the maintenance .service MUST
    # propagate as a nonzero exit so an operator sees the cleanup failed.
    maintenance_path = tmp_path / "etc/systemd/system/libvirt-backup-system-maintenance.service"
    maintenance_path.parent.mkdir(parents=True)
    maintenance_path.write_text("stale-maintenance\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == maintenance_path:
            raise PermissionError("no perms")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert install(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "failed to remove stale systemd unit" in err
    assert str(maintenance_path) in err
