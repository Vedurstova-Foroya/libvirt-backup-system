from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.cli import main


def test_cli_start_dispatches_to_systemd_units(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start(prefix: str | None, *, config_path: str | None = None) -> int:
        captured["args"] = (prefix, config_path)
        return 8

    monkeypatch.setattr("libvirt_backup_system.cli.start", fake_start)
    custom = str(tmp_path / "custom.env")
    assert main(["--config", custom, "--prefix", "/x", "start"]) == 8
    assert captured["args"] == ("/x", custom)
