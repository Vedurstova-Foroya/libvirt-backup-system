from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import kopia_repo, kopia_snapshots, list_restore_points, restore
from libvirt_backup_system.config import Config

from .conftest import ALPHA_UUID
from .restore_helpers import TIMESTAMP, make_config, make_manifest, make_row, rows_result


def _meta_snapshot(*, host_id: str = "host-b") -> kopia_snapshots.KopiaSnapshot:
    return kopia_snapshots.KopiaSnapshot(
        snapshot_id="meta-peer",
        source_host="host",
        source_user="root",
        source_path=f"libvirt-backup:{ALPHA_UUID}/meta",
        start_time="2026-01-01T01:01:01Z",
        end_time="2026-01-01T01:01:01Z",
        tags={
            "kind": "meta",
            "vm-uuid": ALPHA_UUID,
            "run-id": "run-1",
            "timestamp": TIMESTAMP,
            "host": host_id,
            "vm-name": "peer-vm",
        },
        root_entry_id="root",
    )


def _stub_peer_repo(monkeypatch: pytest.MonkeyPatch, cfg: Config, peer_cfg: Path) -> None:
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: None)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: cfg.prefix / "cache")
    monkeypatch.setattr(
        kopia_repo,
        "discover_peer_repos",
        lambda _cfg: [kopia_repo.PeerRepo("host-b", cfg.path_value("BACKUP_PATH") / "host-b" / "kopia-repo", peer_cfg)],
    )


def test_list_restore_points_succeeds_with_peer_rows_when_local_repo_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path)
    peer_cfg = tmp_path / "peer.config"
    _stub_peer_repo(monkeypatch, cfg, peer_cfg)
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: peer_cfg)
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_kw: [_meta_snapshot()])

    assert list_restore_points.list_restore_points(cfg, json_output=True) == 0
    assert '"source_host_id": "host-b"' in capsys.readouterr().out


def test_list_restore_points_fails_when_peer_repo_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    _stub_peer_repo(monkeypatch, cfg, tmp_path / "peer.config")
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: None)

    assert list_restore_points.list_restore_points(cfg, json_output=True) == 1


def test_restore_uses_peer_match_when_local_repo_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    peer_row = make_row(tmp_path, host_id="host-b")
    monkeypatch.setattr(
        restore,
        "enumerate_backups_result",
        lambda _cfg, *, vm_uuid=None: rows_result([peer_row], ok=False, failed_host_ids=("host-a",)),
    )
    monkeypatch.setattr(
        restore,
        "_restore_manifest",
        lambda *_a, **_kw: make_manifest(host_id="host-b", vm_state="shut off"),
    )
    monkeypatch.setattr(restore, "_materialize_disks", lambda *_a, **_kw: True)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)

    assert restore.restore(make_config(tmp_path), ALPHA_UUID, TIMESTAMP) == 0


def test_restore_fails_when_selected_peer_repo_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        restore,
        "enumerate_backups_result",
        lambda _cfg, *, vm_uuid=None: rows_result([], ok=False, failed_host_ids=("host-b",)),
    )
    monkeypatch.setattr(restore, "_ensure_staging_root", lambda *_a, **_kw: pytest.fail("must stop early"))

    assert restore.restore(make_config(tmp_path), ALPHA_UUID, TIMESTAMP, host_id="host-b") == 1
