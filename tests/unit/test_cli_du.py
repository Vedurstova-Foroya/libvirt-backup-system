from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.cli import main
from libvirt_backup_system.config import DEFAULTS, Config

VM_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _fake_config(tmp_path: Path) -> Config:
    cfg = Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_cli_du_forwards_filters(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    seen: dict[str, object] = {}
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)

    def fake_backup_usage(config, *, host_id=None, vm_uuid=None, json_output=False):
        seen.update({"config": config, "host_id": host_id, "json_output": json_output, "vm_uuid": vm_uuid})
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.backup_usage.backup_usage", fake_backup_usage)

    assert main(["du", "--host-id", "host-b", "--vm-uuid", VM_UUID, "--json"]) == 0
    assert seen == {"config": cfg, "host_id": "host-b", "json_output": True, "vm_uuid": VM_UUID}


def test_cli_du_returns_validate_failure(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 7)

    assert main(["du"]) == 7


def test_cli_du_requires_runtime_backup_path(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.runtime_backup_path_ok", lambda config: False)

    assert main(["du"]) == 1
