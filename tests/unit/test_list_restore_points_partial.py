"""Partial peer enumeration coverage for ``list_restore_points`` + ``restore``.

The migration plan (Risks and open questions / "Peer-repo discovery on NFS")
calls out the scenario where one peer host is mid-write when we walk
``BACKUP_PATH/*/kopia-repo/``: kopia's index format tolerates partial state
by ignoring incomplete index files, so ``kopia snapshot list --json`` against
the mid-write repo simply returns an empty array — not an error. This file
verifies the discovery layer treats that case as a normal empty result (not
a failure) and that ``restore._match_row`` cleanly returns ``None`` when
the requested timestamp happens to live on the peer whose write hadn't
completed.

A pinned ``kopia snapshot list`` fixture (``kopia_snapshot_list_empty.json``)
documents the literal payload kopia emits for an empty repo so tests anchor
on the same bytes the binary produces.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from libvirt_backup_system import kopia_repo, kopia_snapshots, list_restore_points, restore
from libvirt_backup_system.config import Config

FIXTURE_DIR = Path(__file__).parent / "fixtures"
EMPTY_FIXTURE = FIXTURE_DIR / "kopia_snapshot_list_empty.json"

ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RUN_ID = "11111111-1111-1111-1111-111111111111"
POPULATED_TIMESTAMP = "20260521T023001"
MIDWRITE_TIMESTAMP = "20260522T023001"


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "HOST_ID": "host-a",
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    (tmp_path / "backups").mkdir(parents=True, exist_ok=True)
    return cfg


def _populated_snapshot() -> kopia_snapshots.KopiaSnapshot:
    return kopia_snapshots.KopiaSnapshot(
        snapshot_id="snap-populated",
        source_host="host",
        source_user="root",
        source_path=f"libvirt-backup:{ALPHA_UUID}/meta",
        start_time="2026-05-21T02:30:01Z",
        end_time="2026-05-21T02:30:05Z",
        tags={
            "kind": "meta",
            "vm-uuid": ALPHA_UUID,
            "run-id": RUN_ID,
            "timestamp": POPULATED_TIMESTAMP,
            "host": "host-b",
            "vm-name": "alpha",
        },
        root_entry_id="r1",
    )


def _stub_two_peers(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    local_cfg: Path,
    populated_cfg: Path,
    midwrite_cfg: Path,
) -> None:
    """Wire up local repo + two peers (one populated, one mid-write/empty)."""
    local_cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: local_cfg)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: cfg.prefix / "cache")
    base = cfg.path_value("BACKUP_PATH")
    peers = [
        kopia_repo.PeerRepo("host-b", base / "host-b" / "kopia-repo", populated_cfg),
        kopia_repo.PeerRepo("host-c", base / "host-c" / "kopia-repo", midwrite_cfg),
    ]
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _cfg: peers)
    monkeypatch.setattr(
        kopia_repo,
        "ensure_peer_connected",
        lambda _cfg, host_id: next((peer.config_file for peer in peers if peer.host_id == host_id), None),
    )


def _stub_snapshot_list_with_empty_peer(
    monkeypatch: pytest.MonkeyPatch,
    local_cfg: Path,
    populated_cfg: Path,
    midwrite_cfg: Path,
) -> None:
    """Populated peer returns one ``kind:meta`` snapshot; mid-write peer returns ``[]``.

    The local repo also has zero ``kind:meta`` rows (so the test focuses on the
    peer enumeration path); this is identical to a freshly-installed host that
    hasn't completed its first local run yet.
    """
    empty_payload = json.loads(EMPTY_FIXTURE.read_text(encoding="utf-8"))
    assert empty_payload == [], "kopia_snapshot_list_empty.json must be the literal '[]' kopia emits"

    def fake_list(
        *,
        config_file: Path,
        password_file: Path,
        cache_dir: Path | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> list[kopia_snapshots.KopiaSnapshot]:
        if config_file == populated_cfg:
            return [_populated_snapshot()]
        if config_file in {midwrite_cfg, local_cfg}:
            return []
        raise AssertionError(f"unexpected config_file: {config_file}")

    monkeypatch.setattr(kopia_snapshots, "snapshot_list", fake_list)


def _wire_partial_peer_scenario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Config, Path, Path, Path]:
    cfg = _make_config(tmp_path)
    local_cfg = tmp_path / "host-a.kopia.config"
    populated_cfg = tmp_path / "host-b.kopia.config"
    midwrite_cfg = tmp_path / "host-c.kopia.config"
    _stub_two_peers(monkeypatch, cfg, local_cfg, populated_cfg, midwrite_cfg)
    _stub_snapshot_list_with_empty_peer(monkeypatch, local_cfg, populated_cfg, midwrite_cfg)
    return cfg, local_cfg, populated_cfg, midwrite_cfg


def test_enumerate_backups_result_treats_empty_peer_as_ok_with_only_populated_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-write peer that returns ``[]`` is a valid kopia response, not a failure."""
    cfg, _local, _populated, _midwrite = _wire_partial_peer_scenario(tmp_path, monkeypatch)

    result = list_restore_points.enumerate_backups_result(cfg)

    assert result.ok is True
    assert result.failed_host_ids == ()
    assert [row.host_id for row in result.rows] == ["host-b"]
    assert result.rows[0].timestamp == POPULATED_TIMESTAMP


def test_list_restore_points_succeeds_with_partial_peer_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI entry point prints only the populated peer's rows and returns 0."""
    cfg, _local, _populated, _midwrite = _wire_partial_peer_scenario(tmp_path, monkeypatch)

    assert list_restore_points.list_restore_points(cfg, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [entry["source_host_id"] for entry in payload] == ["host-b"]
    assert payload[0]["timestamp"] == POPULATED_TIMESTAMP


def test_match_row_returns_none_when_requested_timestamp_lives_on_empty_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A timestamp the operator believes lives on the mid-write peer simply has no match.

    Because ``result.ok`` is True (no peer "failed"), ``_match_row`` does NOT
    log the "enumeration incomplete" event reserved for actual peer failures.
    The caller (``restore.restore``) then emits "found no backup matching uuid
    and timestamp" instead. This is the "fail cleanly, operator retries"
    behaviour the migration plan promises.
    """
    cfg, _local, _populated, _midwrite = _wire_partial_peer_scenario(tmp_path, monkeypatch)

    assert restore._match_row(cfg, ALPHA_UUID, MIDWRITE_TIMESTAMP) is None
    err = capsys.readouterr().err
    assert "restore backup enumeration incomplete" not in err


def test_match_row_finds_row_on_populated_peer_when_mid_write_peer_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check: rows from the healthy peer are still selectable by timestamp."""
    cfg, _local, _populated, _midwrite = _wire_partial_peer_scenario(tmp_path, monkeypatch)

    row = restore._match_row(cfg, ALPHA_UUID, POPULATED_TIMESTAMP)
    assert row is not None
    assert row.host_id == "host-b"
    assert row.snapshot_id == "snap-populated"
