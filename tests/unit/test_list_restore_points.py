from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import enumerate_backups, format_rows, list_restore_points
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


def _seed_chain(  # noqa: PLR0913
    cfg: Config, host_id: str, vm_uuid: str, vm_name: str, month: str, chain_id: str, *, level: str = "full"
) -> Path:
    chain_dir = cfg.path_value("BACKUP_PATH") / host_id / vm_uuid / month / chain_id
    chain_dir.mkdir(parents=True)
    (chain_dir / f"vda.{level}.data").write_bytes(b"x")
    # virtnbdbackup writes ``<vm>.cpt`` next to every chain's data files;
    # ``_read_vm_name`` derives the name from that filename.
    (chain_dir / f"{vm_name}.cpt").write_text("[]", encoding="utf-8")
    return chain_dir


def _seed_running_chain(  # noqa: PLR0913
    cfg: Config, host_id: str, vm_uuid: str, vm_name: str, month: str, chain_id: str
) -> Path:
    return _seed_chain(cfg, host_id, vm_uuid, vm_name, month, chain_id, level="full")


def _append_run(chain_dir: Path, ts: str, checkpoint: str) -> None:
    with (chain_dir / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": ts, "checkpoint": checkpoint}) + "\n")


def _no_mount(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_enumerate_emits_one_row_per_run_record(backup_config: Config) -> None:
    # A modern chain with N runs.jsonl records produces N rows: every recorded
    # checkpoint is restorable, so list-restore-points must surface each of them.
    cfg = _no_mount(backup_config)
    chain_id = "20260501T080000"
    chain_dir = _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", chain_id)
    _append_run(chain_dir, "20260501T080000", "virtnbdbackup.0")
    _append_run(chain_dir, "20260502T120000", "virtnbdbackup.1")
    _append_run(chain_dir, "20260503T100000", "virtnbdbackup.2")

    rows = enumerate_backups(cfg)
    assert [r.timestamp for r in rows] == ["20260501T080000", "20260502T120000", "20260503T100000"]
    assert {r.checkpoint for r in rows} == {"virtnbdbackup.0", "virtnbdbackup.1", "virtnbdbackup.2"}
    assert all(r.vm_name == "alpha" for r in rows)
    assert all(r.host_id == "host" for r in rows)


def test_enumerate_legacy_chain_emits_chain_end_row(backup_config: Config) -> None:
    # A chain without runs.jsonl shows up as a single chain-end row identified
    # by the chain_id timestamp.
    cfg = _no_mount(backup_config)
    chain_id = "20260501T080000"
    _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", chain_id)

    rows = enumerate_backups(cfg)
    assert len(rows) == 1
    assert rows[0].timestamp == chain_id
    assert rows[0].checkpoint is None
    assert rows[0].kind == "full"


def test_enumerate_walks_across_host_directories(backup_config: Config) -> None:
    # Cross-host visibility: a recovery host can list backups taken on another
    # host. Rows must be sorted by (host_id, vm_uuid, timestamp).
    cfg = _no_mount(backup_config)
    _seed_running_chain(cfg, "host-a", ALPHA_UUID, "alpha", "2026-05", "20260501T080000")
    _seed_running_chain(cfg, "host-b", BETA_UUID, "beta", "2026-05", "20260502T080000")

    rows = enumerate_backups(cfg)
    assert [r.host_id for r in rows] == ["host-a", "host-b"]


def test_enumerate_filters_to_vm_uuid(backup_config: Config) -> None:
    cfg = _no_mount(backup_config)
    _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", "20260501T080000")
    _seed_running_chain(cfg, "host", BETA_UUID, "beta", "2026-05", "20260502T080000")

    rows = enumerate_backups(cfg, vm_uuid=ALPHA_UUID)
    assert [r.vm_uuid for r in rows] == [ALPHA_UUID]


def test_enumerate_skips_non_chain_entries(backup_config: Config) -> None:
    # Foreign files dropped into the backup tree (operator notes, partial
    # cleanup leftovers) must not derail enumeration: only directories named
    # like YYYYMMDDTHHMMSS under a YYYY-MM dir under a UUID dir under a host
    # dir count as chains.
    cfg = _no_mount(backup_config)
    _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", "20260501T080000")
    (cfg.path_value("BACKUP_PATH") / "host" / ALPHA_UUID / "README").write_text("note", encoding="utf-8")
    (cfg.path_value("BACKUP_PATH") / "host" / ALPHA_UUID / "2026-05" / "stray-file").write_text("x", encoding="utf-8")
    (cfg.path_value("BACKUP_PATH") / "host" / ALPHA_UUID / "2026-05" / "not-a-stamp").mkdir()
    (cfg.path_value("BACKUP_PATH") / "host" / ALPHA_UUID / "2026-05" / ".hidden").mkdir()

    rows = enumerate_backups(cfg)
    assert len(rows) == 1


def test_format_rows_first_two_columns_are_copyable(backup_config: Config) -> None:
    # The first two whitespace-separated tokens of each data row must be
    # ``<uuid> <timestamp>``: that is what the operator copies into restore.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", "20260501T080000")
    _append_run(chain_dir, "20260501T080000", "virtnbdbackup.0")

    rendered = format_rows(enumerate_backups(cfg))
    lines = rendered.splitlines()
    assert lines[0].split()[:2] == ["VM_UUID", "TIMESTAMP"]
    assert lines[1].split()[:2] == [ALPHA_UUID, "20260501T080000"]


def test_list_restore_points_prints_table(backup_config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _no_mount(backup_config)
    chain_dir = _seed_running_chain(cfg, "host", ALPHA_UUID, "alpha", "2026-05", "20260501T080000")
    _append_run(chain_dir, "20260501T080000", "virtnbdbackup.0")

    assert list_restore_points(cfg) == 0
    captured = capsys.readouterr()
    assert ALPHA_UUID in captured.out
    assert "20260501T080000" in captured.out


def test_list_restore_points_no_backups(backup_config: Config, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = _no_mount(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()

    assert list_restore_points(cfg) == 0
    assert "no backups found" in capsys.readouterr().out
