from __future__ import annotations

import contextlib
from pathlib import Path

from libvirt_backup_system.cli import main
from libvirt_backup_system.config import DEFAULTS, Config


def _fake_config(tmp_path: Path) -> Config:
    return Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)


@contextlib.contextmanager
def _fake_lock(_config: object):
    yield Path("/tmp/fake.lock")


def test_cli_verify_preserves_empty_include_host_entries(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, list[str] | None] = {}

    def fake_verify(_config: Config, *, include_hosts: list[str] | None = None) -> int:
        seen["include_hosts"] = include_hosts
        return 0

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", _fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.verify", fake_verify)

    assert main(["verify", "--include-hosts=host-a,,host-b"]) == 0
    assert seen["include_hosts"] == ["host-a", "", "host-b"]


def test_cli_verify_passes_empty_include_hosts_value_to_validator(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, list[str] | None] = {}

    def fake_verify(_config: Config, *, include_hosts: list[str] | None = None) -> int:
        seen["include_hosts"] = include_hosts
        return 0

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", _fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.verify", fake_verify)

    assert main(["verify", "--include-hosts="]) == 0
    assert seen["include_hosts"] == [""]
