"""Shared fixtures + stubs for the doctor test suite."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import doctor
from libvirt_backup_system.config import Config, prefixed
from libvirt_backup_system.shell import CommandResult


def make_config(tmp_path: Path, *, with_backup_path: bool = True) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups") if with_backup_path else "",
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": "host-a",
            "REQUIRE_ROOT": "false",
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    return cfg


def stub_preflight(monkeypatch: pytest.MonkeyPatch, failures: list[str] | None = None) -> None:
    """Per spec, stub ``collect_check_failures`` to return ([], 1, 100)."""
    monkeypatch.setattr(doctor, "collect_check_failures", lambda *_a, **_kw: (list(failures or []), 1, 100))
    monkeypatch.setattr(doctor, "host_id_drift_failures", lambda _cfg: [])


def stub_systemctl_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: False)


def make_install_files(cfg: Config) -> None:
    """Materialize the wrapper, package, and config file under the prefixed paths."""
    root = cfg.prefix
    wrapper = prefixed(doctor.WRAPPER_PATH, root)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    wrapper.chmod(0o755)
    package = prefixed(doctor.PACKAGE_PATH, root)
    package.mkdir(parents=True, exist_ok=True)
    config_file = cfg.path
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(cfg.render_env(), encoding="utf-8")


def write_unit(cfg: Config, name: str, content: str = "x") -> Path:
    systemd_dir = prefixed(doctor.SYSTEMD_DIR, cfg.prefix)
    systemd_dir.mkdir(parents=True, exist_ok=True)
    unit_path = systemd_dir / name
    unit_path.write_text(content, encoding="utf-8")
    return unit_path


def stub_systemctl_values(monkeypatch: pytest.MonkeyPatch, values: dict[tuple[str, str], str]) -> None:
    """Patch ``doctor.run`` so ``_systemctl_value(unit, prop)`` returns ``values[(unit, prop)]``."""

    def fake_run(args: list[str], **_kwargs: Any) -> CommandResult:
        unit = args[2]
        prop = args[3].split("=", 1)[1]
        return CommandResult(args, 0, values.get((unit, prop), ""), "")

    monkeypatch.setattr(doctor, "run", fake_run)
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: True)
