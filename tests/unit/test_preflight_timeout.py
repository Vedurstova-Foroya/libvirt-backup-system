from __future__ import annotations

from libvirt_backup_system.preflight import check

from .test_preflight import _preflight_config, patch_valid_preflight


def test_command_timeout_must_be_positive(monkeypatch, capsys, backup_config) -> None:
    cfg = _preflight_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    cfg.values["COMMAND_TIMEOUT_SECONDS"] = "0"
    patch_valid_preflight(monkeypatch)
    assert check(cfg) == 1
    assert "COMMAND_TIMEOUT_SECONDS must be greater than 0" in capsys.readouterr().err
