from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.installer import uninstall


def test_uninstall_purges_file_path(tmp_path: Path) -> None:
    state_file = tmp_path / "var/lib/libvirt-backup-system"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("state\n", encoding="utf-8")
    assert uninstall(str(tmp_path), purge_state=True) == 0
    assert not state_file.exists()


def test_uninstall_ignores_missing_purge_path(tmp_path: Path) -> None:
    assert uninstall(str(tmp_path), purge_logs=True) == 0


def test_uninstall_reports_invalid_command_timeout(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("COMMAND_TIMEOUT_SECONDS=0\n", encoding="utf-8")
    assert uninstall(str(tmp_path)) == 1
    assert "invalid command timeout" in capsys.readouterr().err


def test_uninstall_purge_config_removes_custom_config_path(tmp_path: Path) -> None:
    default_config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    default_config.parent.mkdir(parents=True)
    default_config.write_text("BACKUP_PATH=\n", encoding="utf-8")
    custom_config = tmp_path / "custom/libvirt-backup.env"
    custom_config.parent.mkdir(parents=True)
    custom_config.write_text("BACKUP_PATH=\n", encoding="utf-8")

    assert uninstall(str(tmp_path), config_path=str(custom_config), purge_config=True) == 0
    assert not custom_config.exists()
    assert custom_config.parent.exists()
    assert default_config.exists()


def test_uninstall_purge_config_keeps_sibling_files_under_default_dir(tmp_path: Path) -> None:
    default_config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    default_config.parent.mkdir(parents=True)
    default_config.write_text("BACKUP_PATH=\n", encoding="utf-8")
    sibling_file = default_config.parent / "operator-notes.txt"
    sibling_file.write_text("kept across reinstalls\n", encoding="utf-8")
    sibling_dir = default_config.parent / "drop-ins"
    sibling_dir.mkdir()
    (sibling_dir / "00-extra.conf").write_text("EXTRA=1\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_config=True) == 0
    assert not default_config.exists()
    assert sibling_file.read_text(encoding="utf-8") == "kept across reinstalls\n"
    assert (sibling_dir / "00-extra.conf").read_text(encoding="utf-8") == "EXTRA=1\n"


def test_uninstall_returns_nonzero_when_opt_rmtree_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    opt_dir = tmp_path / "opt/libvirt-backup-system"
    opt_dir.mkdir(parents=True)

    def failing_rmtree(path: object) -> None:
        raise OSError("opt removal failed")

    monkeypatch.setattr("libvirt_backup_system.installer.shutil.rmtree", failing_rmtree)
    assert uninstall(str(tmp_path)) == 1
    assert "failed to remove directory" in capsys.readouterr().err
    assert opt_dir.exists()


def test_uninstall_returns_nonzero_when_purge_rmtree_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    state_dir = tmp_path / "var/lib/libvirt-backup-system"
    state_dir.mkdir(parents=True)

    def failing_rmtree(path: object) -> None:
        raise OSError("purge denied")

    monkeypatch.setattr("libvirt_backup_system.installer.shutil.rmtree", failing_rmtree)
    assert uninstall(str(tmp_path), purge_state=True) == 1
    assert "purge failed" in capsys.readouterr().err


def test_uninstall_returns_nonzero_when_purge_unlink_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    log_file = tmp_path / "var/log/libvirt-backup-system"
    log_file.parent.mkdir(parents=True)
    log_file.write_text("log\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == log_file:
            raise OSError("unlink denied")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert uninstall(str(tmp_path), purge_logs=True) == 1
    assert "purge failed" in capsys.readouterr().err


def test_uninstall_returns_nonzero_when_systemctl_fails(tmp_path: Path, monkeypatch) -> None:
    from libvirt_backup_system.shell import CommandResult

    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", lambda path, root: tmp_path / str(path).lstrip("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.run",
        lambda args, check=True, env=None: CommandResult(args, 1, "", "boom"),
    )
    assert uninstall(None) == 1


def test_uninstall_continues_when_unlink_raises_permission_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")

    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == bin_path:
            raise PermissionError("no perms")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert uninstall(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "failed to remove file" in err
    assert bin_path.exists()
