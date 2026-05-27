from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo
from libvirt_backup_system.config import Config


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update({"BACKUP_PATH": str(tmp_path / "backup"), "HOST_ID": "host-a"})
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.mark.parametrize("host_id", ["", "   ", "bad/host", "bad\\host", ".", "..", "bad host", "bad\x7fhost"])
def test_ensure_peer_connected_rejects_unsafe_host_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_id: str
) -> None:
    cfg = _make_config(tmp_path)

    def fail_connect(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("unsafe peer host must not reach kopia")

    monkeypatch.setattr(kopia_client, "repository_connect_filesystem", fail_connect)

    assert kopia_repo.ensure_peer_connected(cfg, host_id) is None


def test_peer_host_id_failure_accepts_safe_name() -> None:
    assert kopia_repo.peer_host_id_failure("host-01.example") is None
