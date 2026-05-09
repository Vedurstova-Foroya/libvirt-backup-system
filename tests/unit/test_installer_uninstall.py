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


def test_uninstall_skips_backup_purge_when_path_unset(tmp_path: Path, capsys) -> None:
    assert uninstall(str(tmp_path), purge_backups=True) == 0
    assert "backup purge skipped" in capsys.readouterr().err


def test_uninstall_skips_unsafe_backup_purge(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f"BACKUP_PATH={tmp_path / 'backups'}\nHOST_ID=../outside\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_backups=True) == 0
    assert (outside / "keep").exists()
    assert "backup purge skipped because backup path is unsafe" in capsys.readouterr().err


def test_uninstall_purges_safe_backup_path(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f"BACKUP_PATH={tmp_path / 'backups'}\nHOST_ID=host\n",
        encoding="utf-8",
    )
    backup_root = tmp_path / "backups/host"
    backup_root.mkdir(parents=True)
    (backup_root / "backup").write_text("backup\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_backups=True) == 0
    assert not backup_root.exists()


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
    assert uninstall(str(tmp_path)) == 0
    err = capsys.readouterr().err
    assert "failed to remove file" in err
    assert bin_path.exists()
