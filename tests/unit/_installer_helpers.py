"""Shared fixtures for the ``installer.install`` test files.

Kept under the ``tests.unit`` package so ``test_installer_systemd.py`` and
``test_installer_render_units.py`` can import the same Config + systemd-root
stubs without re-declaring them. Split out to keep each test file under
the project's 300-LOC ceiling.
"""

from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config


def fake_config_factory(
    tmp_path: Path,
    *,
    backup_path: str | None = None,
    calendar: str | None = None,
    timeout_seconds: str | None = None,
) -> object:
    def fake_config(
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        _ = (config_path, prefix, apply_env_overrides)
        values = dict(DEFAULTS)
        values["HOST_ID"] = "host-a"
        if backup_path is not None:
            values["BACKUP_PATH"] = backup_path
        if calendar is not None:
            values["SYSTEMD_ON_CALENDAR"] = calendar
        if timeout_seconds is not None:
            values["COMMAND_TIMEOUT_SECONDS"] = timeout_seconds
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    return fake_config


def fake_systemd_root(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    fake_prefixed = lambda path, root: tmp_path / str(path).lstrip("/")  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.installer_uninstall.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.prefixed", fake_prefixed)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.default_config_path",
        lambda root=None: tmp_path / "etc/config.env",
    )
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
