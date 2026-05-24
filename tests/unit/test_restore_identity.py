from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from libvirt_backup_system import kopia_snapshots, restore
from libvirt_backup_system.list_restore_points import BackupRow

from .conftest import ALPHA_UUID, BETA_UUID
from .restore_helpers import TIMESTAMP, Snap, make_config, make_manifest, make_row


def test_match_row_rejects_duplicate_timestamp_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path)
    first = make_row(tmp_path)
    second = BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp=TIMESTAMP,
        host_id="host-b",
        vm_name="myvm",
        run_id="run-2",
        snapshot_id="def456",
        config_file=tmp_path / "peer.config",
    )
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [first, second])
    assert restore._match_row(cfg, ALPHA_UUID, TIMESTAMP) is None
    assert "matched multiple backups" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("field", "manifest_value"),
    [
        ("vm_uuid", BETA_UUID),
        ("timestamp", "20250101T000000"),
        ("host_id", "host-b"),
        ("run_id", "run-2"),
    ],
)
def test_restore_rejects_manifest_that_does_not_match_selected_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    manifest_value: str,
) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    manifest = replace(make_manifest(), **{field: manifest_value})
    monkeypatch.setattr(restore, "_restore_manifest", lambda *_a, **_k: manifest)
    monkeypatch.setattr(restore, "_local_domain_name_for_uuid", lambda *_a, **_k: pytest.fail("must stop early"))
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    assert "manifest does not match selected restore point" in capsys.readouterr().err


def test_disk_snapshot_id_rejects_duplicate_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [Snap("one"), Snap("two")])
    assert restore._disk_snapshot_id(make_config(tmp_path), make_row(tmp_path), "vda") is None
    assert "matched multiple snapshots" in capsys.readouterr().err
