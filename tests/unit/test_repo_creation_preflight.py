from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.installer import install
from libvirt_backup_system.kopia_password import PasswordSpec
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
    assert not (tmp_path / "usr/local/bin/libvirt-backup-system").exists()
    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.service").exists()


def test_install_refuses_repo_preflight_before_writing_first_password(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    machine_id = tmp_path / "etc/machine-id"
    machine_id.parent.mkdir(parents=True)
    machine_id.write_text("11111111111111111111111111111111\n", encoding="utf-8")
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))
    monkeypatch.setenv("BACKUP_REQUIRE_NFS_MOUNT", "true")
    assert install(str(tmp_path), password_spec=PasswordSpec(literal="new-pw", acknowledge_loss=True)) == 1
    assert not (tmp_path / "etc/libvirt-backup-system/kopia.pw").exists()
    assert not (tmp_path / "usr/local/bin/libvirt-backup-system").exists()


def test_install_refuses_to_create_repo_with_unsafe_host_id(tmp_path: Path, monkeypatch) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\nHOST_ID=../elsewhere\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)
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
    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.service").exists()


def test_start_refuses_to_create_repo_with_unsafe_host_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(f"BACKUP_PATH={backup_dir}\nHOST_ID=../elsewhere\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("repo setup must not run")),
    )
    assert start(str(tmp_path)) == 1
