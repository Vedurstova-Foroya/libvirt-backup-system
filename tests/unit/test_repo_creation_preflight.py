from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.installer import install
from libvirt_backup_system.systemd_start import start
from tests.unit.conftest import write_kopia_password_file


def test_install_refuses_to_create_repo_when_required_mount_is_absent(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    write_kopia_password_file(tmp_path)
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))
    monkeypatch.setenv("BACKUP_REQUIRE_NFS_MOUNT", "true")
    monkeypatch.setattr(
        "libvirt_backup_system.installer.kopia_repo.ensure_local_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("repo setup must not run")),
    )
    assert install(str(tmp_path)) == 1


def test_start_refuses_to_create_repo_when_required_mount_is_absent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(
        f"BACKUP_PATH={backup_dir}\nBACKUP_REQUIRE_NFS_MOUNT=true\n",
        encoding="utf-8",
    )
    write_kopia_password_file(tmp_path)
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("repo setup must not run")),
    )
    assert start(str(tmp_path)) == 1
