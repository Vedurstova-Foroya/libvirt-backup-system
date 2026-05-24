"""Unit tests for :mod:`libvirt_backup_system.list_restore_points`.

External surface (``kopia_repo.iter_connected_peers``,
``kopia_repo.local_config_file``, ``kopia_repo.password_file_path``,
``kopia_repo.cache_dir``, ``kopia_snapshots.snapshot_list``) is stubbed
so each test exercises ``enumerate_backups`` / ``format_rows`` in
isolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from libvirt_backup_system import kopia_repo, kopia_snapshots, list_restore_points
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult

ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BETA_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RUN_ID_A = "11111111-1111-1111-1111-111111111111"


def _make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "HOST_ID": host_id,
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
        }
    )
    (tmp_path / "backups").mkdir(parents=True, exist_ok=True)
    return cfg


def _snapshot(
    *,
    snap_id: str = "snap-1",
    start_time: str = "2026-05-21T02:30:01Z",
    vm_uuid: str = ALPHA_UUID,
    vm_name: str = "alpha",
    run_id: str = RUN_ID_A,
    timestamp_tag: str | None = None,
) -> kopia_snapshots.KopiaSnapshot:
    tags = {}
    if vm_uuid:
        tags["vm-uuid"] = vm_uuid
    if run_id:
        tags["run-id"] = run_id
    if vm_name:
        tags["vm-name"] = vm_name
    if timestamp_tag is not None:
        tags["timestamp"] = timestamp_tag
    tags["kind"] = "meta"
    return kopia_snapshots.KopiaSnapshot(
        snapshot_id=snap_id,
        source_host="host",
        source_user="root",
        source_path="libvirt-backup:" + vm_uuid + "/meta",
        start_time=start_time,
        end_time=start_time,
        tags=tags,
        root_entry_id="r1",
    )


def _stub_repo_helpers(
    monkeypatch: pytest.MonkeyPatch,
    cfg: Config,
    *,
    local_config_present: bool = True,
    peers: list[kopia_repo.PeerRepo] | None = None,
) -> Path:
    local_cfg = cfg.prefix / "kopia-local.config"
    if local_config_present:
        local_cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: local_cfg)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: cfg.prefix / "cache")
    monkeypatch.setattr(kopia_repo, "iter_connected_peers", lambda _cfg: peers or [])
    return local_cfg


def _stub_snapshot_list(
    monkeypatch: pytest.MonkeyPatch,
    by_config_file: Mapping[Path, list[kopia_snapshots.KopiaSnapshot]] | None = None,
    *,
    raise_command_error_for: Path | None = None,
    raise_value_error_for: Path | None = None,
) -> None:
    def fake_list(
        *,
        config_file: Path,
        password_file: Path,
        cache_dir: Path | None = None,
        tags: Mapping[str, str] | None = None,
    ) -> list[kopia_snapshots.KopiaSnapshot]:
        if raise_command_error_for is not None and config_file == raise_command_error_for:
            raise CommandError(CommandResult(["kopia"], 1, "", "denied"))
        if raise_value_error_for is not None and config_file == raise_value_error_for:
            raise ValueError("bad data")
        return list((by_config_file or {}).get(config_file, []))

    monkeypatch.setattr(kopia_snapshots, "snapshot_list", fake_list)


def test_timestamp_from_start_normalizes_rfc3339() -> None:
    assert list_restore_points._timestamp_from_start("2026-05-21T02:30:01Z") == "20260521T023001"


def test_timestamp_from_start_handles_subseconds() -> None:
    assert list_restore_points._timestamp_from_start("2026-05-21T02:30:01.123Z") == "20260521T023001"


def test_timestamp_from_start_handles_empty_string() -> None:
    assert list_restore_points._timestamp_from_start("") == ""


def test_timestamp_from_start_preserves_value_without_trailing_z() -> None:
    # Falls back to the raw shape minus dashes/colons so a kopia format change
    # is visible to the operator instead of being silently swallowed.
    assert list_restore_points._timestamp_from_start("2026-05-21T02:30:01+00:00") == "20260521T023001+0000"


def test_local_rows_returns_empty_when_no_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    _stub_repo_helpers(monkeypatch, cfg, local_config_present=False)
    _stub_snapshot_list(monkeypatch)
    rows = list_restore_points._local_rows(cfg)
    assert rows == []


def test_local_rows_emits_rows_for_meta_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    _stub_snapshot_list(monkeypatch, {local_cfg: [_snapshot()]})
    rows = list_restore_points._local_rows(cfg)
    assert len(rows) == 1
    row = rows[0]
    assert row.vm_uuid == ALPHA_UUID
    assert row.host_id == "host-a"
    assert row.vm_name == "alpha"
    assert row.run_id == RUN_ID_A
    assert row.timestamp == "20260521T023001"
    assert row.config_file == local_cfg


def test_local_rows_prefers_timestamp_tag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    _stub_snapshot_list(monkeypatch, {local_cfg: [_snapshot(timestamp_tag="20260101T010101")]})
    assert list_restore_points._local_rows(cfg)[0].timestamp == "20260101T010101"


def test_local_rows_skips_snapshots_missing_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    snaps = [_snapshot(vm_uuid=""), _snapshot(run_id="")]
    _stub_snapshot_list(monkeypatch, {local_cfg: snaps})
    assert list_restore_points._local_rows(cfg) == []


def test_local_rows_allows_older_snapshots_without_vm_name_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    _stub_snapshot_list(monkeypatch, {local_cfg: [_snapshot(vm_name="")]})
    rows = list_restore_points._local_rows(cfg)
    assert len(rows) == 1
    assert rows[0].vm_name == ""


def test_rows_from_repo_returns_empty_on_command_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    _stub_snapshot_list(monkeypatch, raise_command_error_for=local_cfg)
    assert list_restore_points._rows_from_repo(cfg, host_id="host-a", config_file=local_cfg) == []


def test_rows_from_repo_returns_empty_on_value_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    _stub_snapshot_list(monkeypatch, raise_value_error_for=local_cfg)
    assert list_restore_points._rows_from_repo(cfg, host_id="host-a", config_file=local_cfg) == []


def test_peer_rows_skips_local_host_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path, host_id="host-a")
    local_cfg = cfg.prefix / "kopia-local.config"
    local_cfg.write_text("{}", encoding="utf-8")
    peer_cfg = cfg.prefix / "kopia-peer.config"
    peer_cfg.write_text("{}", encoding="utf-8")
    peers = [
        kopia_repo.PeerRepo("host-a", tmp_path / "ra", local_cfg),
        kopia_repo.PeerRepo("host-b", tmp_path / "rb", peer_cfg),
    ]
    _stub_repo_helpers(monkeypatch, cfg, peers=peers)
    _stub_snapshot_list(monkeypatch, {peer_cfg: [_snapshot(snap_id="b-1")]})
    rows = list_restore_points._peer_rows(cfg)
    assert len(rows) == 1
    assert rows[0].host_id == "host-b"


def test_enumerate_backups_combines_local_and_peer_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path, host_id="host-a")
    local_cfg = cfg.prefix / "kopia-local.config"
    local_cfg.write_text("{}", encoding="utf-8")
    peer_cfg = cfg.prefix / "kopia-peer.config"
    peer_cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: local_cfg)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: cfg.prefix / "cache")
    monkeypatch.setattr(
        kopia_repo,
        "iter_connected_peers",
        lambda _cfg: [kopia_repo.PeerRepo("host-b", tmp_path / "rb", peer_cfg)],
    )
    _stub_snapshot_list(
        monkeypatch,
        {
            local_cfg: [_snapshot(snap_id="a-late", start_time="2026-05-21T02:30:01Z")],
            peer_cfg: [_snapshot(snap_id="b-early", start_time="2026-05-20T02:30:01Z", vm_uuid=BETA_UUID)],
        },
    )
    rows = list_restore_points.enumerate_backups(cfg)
    assert [r.snapshot_id for r in rows] == ["a-late", "b-early"]


def test_enumerate_backups_sorts_descending_by_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path, host_id="host-a")
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    snaps = [
        _snapshot(snap_id="older", start_time="2026-05-20T02:30:01Z"),
        _snapshot(snap_id="newer", start_time="2026-05-21T02:30:01Z"),
    ]
    _stub_snapshot_list(monkeypatch, {local_cfg: snaps})
    rows = list_restore_points.enumerate_backups(cfg)
    # Same host, same vm-uuid -> only timestamp order matters; descending.
    assert [row.snapshot_id for row in rows] == ["newer", "older"]


def test_enumerate_backups_filters_by_vm_uuid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path, host_id="host-a")
    local_cfg = _stub_repo_helpers(monkeypatch, cfg)
    snaps = [_snapshot(snap_id="alpha"), _snapshot(snap_id="beta", vm_uuid=BETA_UUID)]
    _stub_snapshot_list(monkeypatch, {local_cfg: snaps})
    rows = list_restore_points.enumerate_backups(cfg, vm_uuid=BETA_UUID)
    assert [row.snapshot_id for row in rows] == ["beta"]
