"""Tests for ``installer._render_units`` error handling.

Kept in its own file so ``test_installer_systemd.py`` stays under the
project's 300-LOC ceiling.
"""

from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.installer import install
from tests.unit._installer_helpers import fake_config_factory as _fake_config_factory
from tests.unit._installer_helpers import fake_systemd_root as _fake_systemd_root
from tests.unit.conftest import stub_ensure_kopia_repo, write_kopia_password_file


def test_install_logs_render_unit_value_error(tmp_path: Path, monkeypatch, capsys) -> None:
    """``_render_units`` ValueErrors surface as "invalid systemd unit path" events."""
    _fake_systemd_root(tmp_path, monkeypatch)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    monkeypatch.setattr(
        "libvirt_backup_system.installer.Config.load",
        _fake_config_factory(tmp_path, backup_path=str(tmp_path / "backups")),
    )

    def boom(*_a: object, **_kw: object) -> str:
        raise ValueError("bin_path must be an absolute path for systemd units: relative")

    monkeypatch.setattr("libvirt_backup_system.installer.render_unit_service", boom)
    assert install(None) == 1
    err = capsys.readouterr().err
    assert "invalid systemd unit path" in err
    assert "bin_path must be an absolute path" in err
