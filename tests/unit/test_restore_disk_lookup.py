from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore

from .conftest import ALPHA_UUID
from .restore_helpers import Snap, make_config, make_row


def test_disk_snapshot_lookup_filters_by_row_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_list(**kwargs: Any) -> list[Snap]:
        if kwargs["tags"] == {
            "kind": "disk",
            "vm-uuid": ALPHA_UUID,
            "run-id": "run-1",
            "disk": "vda",
            "host": "host-b",
        }:
            return []
        return [Snap("wrong-host")]

    monkeypatch.setattr(kopia_snapshots, "snapshot_list", fake_list)

    assert restore._disk_snapshot_id(make_config(tmp_path), make_row(tmp_path, host_id="host-b"), "vda") is None
    assert "disk snapshot missing for run" in capsys.readouterr().err
