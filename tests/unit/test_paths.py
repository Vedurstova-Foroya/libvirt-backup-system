from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.paths import runtime_backup_path_ok


def test_runtime_backup_path_ok_returns_false_when_is_mount_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # A stale NFS handle bubbles OSError out of Path.is_mount(); the helper
    # must catch it and report "no longer a mount point" instead of
    # crashing every caller that re-checks the mount between phases.
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backups")
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    (tmp_path / "backups").mkdir()

    def refuse(self: Path) -> bool:
        raise OSError("ESTALE")

    monkeypatch.setattr("libvirt_backup_system.paths.Path.is_mount", refuse)
    assert runtime_backup_path_ok(cfg) is False
    assert "BACKUP_PATH mount probe failed" in capsys.readouterr().err
