from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.installer import uninstall


def test_uninstall_purge_state_preserves_configured_kopia_password_file_under_state_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "BACKUP_PATH=\nKOPIA_PASSWORD_FILE=/var/lib/libvirt-backup-system/secrets/kopia.pw\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "var/lib/libvirt-backup-system"
    password_file = state_dir / "secrets/kopia.pw"
    password_file.parent.mkdir(parents=True)
    password_file.write_text("secret\n", encoding="utf-8")
    stale_file = state_dir / "host-id"
    stale_file.write_text("old-host\n", encoding="utf-8")
    stale_dir_file = state_dir / "restore/run/tmp.txt"
    stale_dir_file.parent.mkdir(parents=True)
    stale_dir_file.write_text("old restore\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_state=True) == 0

    assert password_file.read_text(encoding="utf-8") == "secret\n"
    assert password_file.parent.exists()
    assert not stale_file.exists()
    assert not stale_dir_file.exists()
    assert not stale_dir_file.parent.exists()


def test_uninstall_purge_logs_preserves_configured_kopia_password_file_under_logs(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "BACKUP_PATH=\nKOPIA_PASSWORD_FILE=/var/log/libvirt-backup-system/secrets/kopia.pw\n",
        encoding="utf-8",
    )
    log_dir = tmp_path / "var/log/libvirt-backup-system"
    password_file = log_dir / "secrets/kopia.pw"
    password_file.parent.mkdir(parents=True)
    password_file.write_text("secret\n", encoding="utf-8")
    stale_log = log_dir / "old.log"
    stale_log.write_text("old log\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_logs=True) == 0

    assert password_file.read_text(encoding="utf-8") == "secret\n"
    assert not stale_log.exists()


def test_uninstall_purge_config_preserves_configured_kopia_password_file(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "KOPIA_PASSWORD_FILE=/etc/libvirt-backup-system/libvirt-backup.env\n",
        encoding="utf-8",
    )

    assert uninstall(str(tmp_path), purge_config=True) == 0

    assert config_path.exists()


def test_uninstall_purge_state_preserves_backup_path_under_state_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "HOST_ID=host-a\nBACKUP_PATH=/var/lib/libvirt-backup-system/backups\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "var/lib/libvirt-backup-system"
    backup_file = state_dir / "backups/host-a/kopia-repo/kopia.repository.f"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("repo\n", encoding="utf-8")
    stale_file = state_dir / "host-id"
    stale_file.write_text("old\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_state=True) == 0

    assert backup_file.read_text(encoding="utf-8") == "repo\n"
    assert not stale_file.exists()


def test_uninstall_purge_state_preserves_configured_kopia_repo_under_state_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "HOST_ID=host-a\n"
        "BACKUP_PATH=/mnt/backups\n"
        "KOPIA_REPO_PATH=/var/lib/libvirt-backup-system/local-kopia-repo\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "var/lib/libvirt-backup-system"
    repo_file = state_dir / "local-kopia-repo/kopia.repository.f"
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text("repo\n", encoding="utf-8")
    stale_file = state_dir / "host-id"
    stale_file.write_text("old\n", encoding="utf-8")

    assert uninstall(str(tmp_path), purge_state=True) == 0

    assert repo_file.read_text(encoding="utf-8") == "repo\n"
    assert not stale_file.exists()
