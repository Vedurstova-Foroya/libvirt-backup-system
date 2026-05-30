from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import install
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.systemd_units import (
    MAINTENANCE_FULL_TIMER_NAME,
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)
from tests.unit.conftest import write_kopia_password_file


def test_install_without_backup_path_disables_kopia_units_before_removing_them(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    def fake_config(
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        del config_path, prefix, apply_env_overrides
        values = dict(DEFAULTS)
        values["BACKUP_PATH"] = ""
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    fake_prefixed = lambda path, root: tmp_path / str(path).lstrip("/")  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.installer_uninstall.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.prefixed", fake_prefixed)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.default_config_path",
        lambda root=None: tmp_path / "etc/config.env",
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", fake_config)
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")

    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    for unit in [
        MAINTENANCE_UNIT_NAME,
        MAINTENANCE_TIMER_NAME,
        MAINTENANCE_FULL_UNIT_NAME,
        MAINTENANCE_FULL_TIMER_NAME,
        VERIFY_UNIT_NAME,
        VERIFY_TIMER_NAME,
    ]:
        (systemd_dir / unit).write_text("stale\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )

    assert install(None) == 0

    assert calls == [
        ["systemctl", "disable", "--now", MAINTENANCE_TIMER_NAME],
        ["systemctl", "stop", MAINTENANCE_UNIT_NAME],
        ["systemctl", "disable", "--now", MAINTENANCE_FULL_TIMER_NAME],
        ["systemctl", "stop", MAINTENANCE_FULL_UNIT_NAME],
        ["systemctl", "disable", "--now", VERIFY_TIMER_NAME],
        ["systemctl", "stop", VERIFY_UNIT_NAME],
        ["systemctl", "daemon-reload"],
    ]
    for unit in [
        MAINTENANCE_UNIT_NAME,
        MAINTENANCE_TIMER_NAME,
        MAINTENANCE_FULL_UNIT_NAME,
        MAINTENANCE_FULL_TIMER_NAME,
        VERIFY_UNIT_NAME,
        VERIFY_TIMER_NAME,
    ]:
        assert not (systemd_dir / unit).exists()


def test_install_without_backup_path_cleans_stale_kopia_units_without_password(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("BACKUP_PATH=\n", encoding="utf-8")
    password_file = tmp_path / "etc/libvirt-backup-system/kopia.pw"
    password_file.write_text("stale\n", encoding="utf-8")
    password_file.chmod(0o644)
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    stale_units = [
        systemd_dir / "libvirt-backup-system-maintenance.service",
        systemd_dir / "libvirt-backup-system-maintenance.timer",
        systemd_dir / "libvirt-backup-system-verify.service",
        systemd_dir / "libvirt-backup-system-verify.timer",
    ]
    for unit in stale_units:
        unit.write_text("stale\n", encoding="utf-8")

    assert install(str(tmp_path)) == 0

    assert all(not unit.exists() for unit in stale_units)
    assert password_file.exists()
