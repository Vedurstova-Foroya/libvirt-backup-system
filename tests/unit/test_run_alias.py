from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import cli
from libvirt_backup_system.config import DEFAULTS, Config


def _fake_config(tmp_path: Path) -> Config:
    return Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)


def test_backup_alias_runs_backup_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _fake_config(tmp_path)
    invoked: list[str] = []

    @contextlib.contextmanager
    def fake_lock(config: object):
        yield config

    monkeypatch.setattr(cli.Config, "load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr(cli, "check", lambda config, *, lock_held=False: 0)
    monkeypatch.setattr(cli, "acquire_run_lock", fake_lock)
    monkeypatch.setattr(cli, "run_backups", lambda config: invoked.append("backup") or 0)

    assert cli.main(["backup"]) == 0
    assert invoked == ["backup"]


def test_backup_alias_dispatches_through_run_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    def fake_dispatch(subcommand: str, *, prefix: object, config_path: object) -> int:
        called["args"] = (subcommand, prefix, config_path)
        return 7

    monkeypatch.setattr(cli, "dispatch_via_systemd", fake_dispatch)
    monkeypatch.setattr(cli.Config, "load", lambda *args, **kwargs: pytest.fail("Config.load must not run"))

    assert cli.main(["backup"]) == 7
    assert called["args"] == ("run", None, None)
