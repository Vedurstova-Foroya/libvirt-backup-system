"""CLI wiring for ``list-restore-points``: validate-config gate before listing."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.cli import main
from libvirt_backup_system.config import DEFAULTS, Config


def _fake_config(tmp_path: Path) -> Config:
    return Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)


def test_cli_list_restore_points_reports_validate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # validate_config gates list-restore-points the same way it does restore:
    # a misconfigured env file must surface as a clean exit code rather than
    # an empty listing that hides the real problem.
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 7)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.list_restore_points",
        lambda config: (_ for _ in ()).throw(AssertionError("must not list when validate fails")),
    )
    assert main(["list-restore-points"]) == 7


def test_cli_list_restore_points_dispatches_after_validate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: _fake_config(tmp_path)
    )
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.list_restore_points", lambda config: 5)
    assert main(["list-restore-points"]) == 5
