"""Tests for ``format_rows`` and the ``list_restore_points`` CLI entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system import list_restore_points
from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import BackupEnumeration

ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RUN_ID_A = "11111111-1111-1111-1111-111111111111"


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "HOST_ID": "host-a",
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
        }
    )
    (tmp_path / "backups").mkdir(parents=True, exist_ok=True)
    return cfg


def _row(tmp_path: Path) -> list_restore_points.BackupRow:
    return list_restore_points.BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp="20260521T023001",
        host_id="host-a",
        vm_name="alpha",
        run_id=RUN_ID_A,
        snapshot_id="snap-1",
        config_file=tmp_path / "k.config",
    )


def test_format_rows_renders_aligned_table(tmp_path: Path) -> None:
    out = list_restore_points.format_rows([_row(tmp_path)])
    lines = out.splitlines()
    assert lines[0].startswith("source-host-id")
    assert lines[0].split() == ["source-host-id", "vm-uuid", "timestamp", "run-id", "consistency", "vm-name"]
    assert ALPHA_UUID in lines[1]
    assert "unknown" in lines[1]
    assert "alpha" in lines[1]
    assert "snap-1" not in out


def test_format_rows_renders_header_only_for_empty_rows() -> None:
    assert list_restore_points.format_rows([]).startswith("source-host-id")


def test_format_json_preserves_spaced_vm_names(tmp_path: Path) -> None:
    row = _row(tmp_path)
    row = list_restore_points.BackupRow(
        vm_uuid=row.vm_uuid,
        timestamp=row.timestamp,
        host_id=row.host_id,
        vm_name="alpha with spaces",
        run_id=row.run_id,
        snapshot_id=row.snapshot_id,
        config_file=row.config_file,
        consistency="crash",
    )
    payload = json.loads(list_restore_points.format_json([row]))
    assert payload[0]["vm_name"] == "alpha with spaces"
    assert payload[0]["timestamp"] == "20260521T023001"
    assert payload[0]["consistency"] == "crash"
    assert payload[0]["source_host_id"] == "host-a"
    assert set(payload[0]) == {"consistency", "run_id", "source_host_id", "timestamp", "vm_name", "vm_uuid"}


def test_list_restore_points_returns_one_when_backup_path_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: False)
    assert list_restore_points.list_restore_points(cfg) == 1


def test_list_restore_points_logs_and_returns_zero_when_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(list_restore_points, "enumerate_backups_result", lambda _cfg: BackupEnumeration([], ok=True))
    assert list_restore_points.list_restore_points(cfg) == 0
    assert "no backups found" in capsys.readouterr().out


def test_list_restore_points_prints_empty_json_when_no_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(list_restore_points, "enumerate_backups_result", lambda _cfg: BackupEnumeration([], ok=True))
    assert list_restore_points.list_restore_points(cfg, json_output=True) == 0
    assert capsys.readouterr().out == "[]\n"


def test_list_restore_points_prints_table_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(
        list_restore_points, "enumerate_backups_result", lambda _cfg: BackupEnumeration([_row(tmp_path)], ok=True)
    )
    assert list_restore_points.list_restore_points(cfg) == 0
    out = capsys.readouterr().out
    assert "snap-1" not in out
    assert "source-host-id" in out


def test_list_restore_points_prints_json_and_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(
        list_restore_points, "enumerate_backups_result", lambda _cfg: BackupEnumeration([_row(tmp_path)], ok=True)
    )
    assert list_restore_points.list_restore_points(cfg, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)[0]["run_id"] == RUN_ID_A


def test_list_restore_points_returns_one_when_enumeration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(list_restore_points, "runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(list_restore_points, "enumerate_backups_result", lambda _cfg: BackupEnumeration([], ok=False))
    assert list_restore_points.list_restore_points(cfg, json_output=True) == 1
    assert capsys.readouterr().out == ""
