from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from libvirt_backup_system.run_records import (
    RUNS_FILE,
    SelectStatus,
    list_checkpoints,
    record_run,
    select_checkpoint,
)

UTC = dt.timezone.utc


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _write_checkpoint(chain_dir: Path, name: str) -> None:
    (chain_dir / f"{name}.checkpoint").write_text("payload\n", encoding="utf-8")


def test_list_checkpoints_only_returns_checkpoint_basenames(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    (chain_dir / "vda.full.data").write_bytes(b"x")
    (chain_dir / "metadata.json").write_text("{}", encoding="utf-8")
    (chain_dir / "nested").mkdir()
    assert list_checkpoints(chain_dir) == {"virtnbdbackup.0", "virtnbdbackup.1"}


def test_list_checkpoints_handles_missing_dir(tmp_path: Path) -> None:
    assert list_checkpoints(tmp_path / "not-there") == set()


def test_list_checkpoints_reads_cpt_file_first(tmp_path: Path) -> None:
    # Real virtnbdbackup writes the authoritative checkpoint list to
    # <chain>/<domain>.cpt as JSON. When that file is present, list_checkpoints
    # must trust it over any leftover legacy .checkpoint files.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "alpha.cpt").write_text(
        json.dumps(["virtnbdbackup.0", "virtnbdbackup.1"]),
        encoding="utf-8",
    )
    # A stray legacy file from a previous tool version: must be ignored once
    # the authoritative .cpt is present.
    (chain_dir / "stale.checkpoint").write_text("ignored\n", encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0", "virtnbdbackup.1"}


def test_list_checkpoints_falls_back_to_checkpoint_xml_dir(tmp_path: Path) -> None:
    # virtnbdbackup also drops per-checkpoint XML under <chain>/checkpoints/.
    # If the .cpt JSON is unreadable but the XML dir is present, use the XMLs
    # as the secondary witness before falling back to the legacy layout.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    (chain_dir / "checkpoints" / "virtnbdbackup.0.xml").write_text("<x/>", encoding="utf-8")
    (chain_dir / "checkpoints" / "virtnbdbackup.1.xml").write_text("<x/>", encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0", "virtnbdbackup.1"}


def test_list_checkpoints_falls_back_to_legacy_when_no_real_state(tmp_path: Path) -> None:
    # Older fixtures and pre-feature chain dirs only have top-level *.checkpoint
    # markers — list_checkpoints must still return them so record_run keeps
    # working through the migration window.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}


def test_list_checkpoints_falls_through_on_corrupt_cpt_file(tmp_path: Path) -> None:
    # A truncated/hand-edited .cpt file (invalid JSON or wrong shape) must not
    # poison the read — fall through to the next witness.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "alpha.cpt").write_text("not-json", encoding="utf-8")
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}
    # Non-list JSON (someone wrote a dict): still fall through.
    (chain_dir / "alpha.cpt").write_text('{"name":"v0"}', encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}


def test_list_checkpoints_xml_dir_empty_falls_through(tmp_path: Path) -> None:
    # The XML dir exists but contains no .xml files (a partially-migrated
    # chain). _read_checkpoint_xml_dir returns None so the reader falls
    # through to the legacy layout.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    (chain_dir / "checkpoints" / "stray.txt").write_text("not xml\n", encoding="utf-8")
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}


def test_select_checkpoint_handles_runs_jsonl_read_failure(tmp_path: Path, monkeypatch) -> None:
    # TOCTOU between is_file() and read_text(): if the file is unreadable
    # by the time we open it, select_checkpoint must not crash. Falling back
    # to MISSING is the conservative answer — the caller will refuse the
    # restore rather than silently restore chain end.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"}) + "\n",
        encoding="utf-8",
    )

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == RUNS_FILE:
            raise OSError("read failed")
        return ""

    monkeypatch.setattr(Path, "read_text", boom)
    selected = select_checkpoint(chain_dir, _at(2026, 1, 5))
    assert selected.checkpoint is None
    assert selected.status is SelectStatus.MISSING


def test_record_run_writes_jsonl_for_each_new_checkpoint(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    before = list_checkpoints(chain_dir)
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    assert record_run(chain_dir, "20260105T120000", before) is True
    lines = (chain_dir / RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"checkpoint": "virtnbdbackup.1", "ts": "20260105T120000"},
    ]


def test_record_run_skips_when_no_new_checkpoint(tmp_path: Path, capsys) -> None:
    # Defensive: an anomalous run that did not add a checkpoint must not write
    # a half-empty record. Returning True keeps the run successful — there is
    # no record for select_checkpoint to disagree with the chain contents.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    before = list_checkpoints(chain_dir)
    assert record_run(chain_dir, "20260105T120000", before) is True
    assert not (chain_dir / RUNS_FILE).exists()
    assert "run recorded no new checkpoint" in capsys.readouterr().out


def test_record_run_appends_to_existing_jsonl(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert record_run(chain_dir, "20260101T080000", set()) is True
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    assert record_run(chain_dir, "20260102T080000", {"virtnbdbackup.0"}) is True
    lines = (chain_dir / RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["checkpoint"] for line in lines] == ["virtnbdbackup.0", "virtnbdbackup.1"]


def test_select_checkpoint_returns_latest_at_or_before(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        "\n".join(
            json.dumps({"ts": ts, "checkpoint": cp})
            for ts, cp in (
                ("20260101T080000", "virtnbdbackup.0"),
                ("20260105T120000", "virtnbdbackup.1"),
                ("20260110T120000", "virtnbdbackup.2"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    # Between recorded runs: picks the latest one whose ts <= target.
    selected = select_checkpoint(chain_dir, _at(2026, 1, 7, 12))
    assert selected.checkpoint == "virtnbdbackup.1"
    assert selected.status is SelectStatus.FOUND
    # Exact match wins.
    selected = select_checkpoint(chain_dir, _at(2026, 1, 5, 12))
    assert selected.checkpoint == "virtnbdbackup.1"
    assert selected.status is SelectStatus.FOUND
    # Older than every record now reports MISSING (was a silent None
    # fallback) so restore can refuse rather than restoring chain end.
    selected = select_checkpoint(chain_dir, _at(2026, 1, 1, 7))
    assert selected.checkpoint is None
    assert selected.status is SelectStatus.MISSING


def test_select_checkpoint_returns_none_at_or_after_last_run(tmp_path: Path) -> None:
    # at >= latest recorded run means "restore the chain end" — omit --until
    # so virtnbdrestore replays everything in the chain dir.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"}) + "\n",
        encoding="utf-8",
    )
    selected_exact = select_checkpoint(chain_dir, _at(2026, 1, 1, 8))
    assert selected_exact.checkpoint is None
    assert selected_exact.status is SelectStatus.CHAIN_END
    selected_after = select_checkpoint(chain_dir, _at(2026, 6, 1))
    assert selected_after.checkpoint is None
    assert selected_after.status is SelectStatus.CHAIN_END


def test_select_checkpoint_returns_legacy_status_when_runs_jsonl_missing(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    # No runs.jsonl at all — legacy chain layout. LEGACY (not MISSING) so the
    # caller knows to fall back to chain-end semantics silently.
    selected = select_checkpoint(chain_dir, _at(2026, 1, 1))
    assert selected.checkpoint is None
    assert selected.status is SelectStatus.LEGACY


def test_record_run_swallows_fsync_directory_failure(tmp_path: Path, monkeypatch) -> None:
    # Parent-dir fsync is best-effort; some NFS configs refuse it. The record
    # itself is still durable, so record_run must return True.
    from libvirt_backup_system import run_records

    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    monkeypatch.setattr(run_records.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no fsync")))
    assert record_run(chain_dir, "20260105T120000", set()) is True


def test_record_run_logs_when_open_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    # If runs.jsonl cannot be opened for append (read-only filesystem, ENOSPC),
    # surface the failure as a logged error AND return False so the caller
    # can fail the backup run rather than silently mis-record state.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")

    def fail(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "open", fail)
    assert record_run(chain_dir, "20260105T120000", set()) is False
    assert "run record write failed" in capsys.readouterr().err


def test_select_checkpoint_ignores_blank_lines(tmp_path: Path) -> None:
    # Blank lines (e.g. trailing newline after a hand-edit) must be skipped
    # without affecting checkpoint selection.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        "\n"
        + json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"})
        + "\n\n\n"
        + json.dumps({"ts": "20260105T120000", "checkpoint": "virtnbdbackup.1"})
        + "\n\n",
        encoding="utf-8",
    )
    selected = select_checkpoint(chain_dir, _at(2026, 1, 3))
    assert selected.checkpoint == "virtnbdbackup.0"
    assert selected.status is SelectStatus.FOUND


def test_select_checkpoint_skips_corrupt_lines(tmp_path: Path) -> None:
    # Truncated/hand-edited lines must not poison the rest of the file.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    lines = [
        json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"}),
        "not-json{",
        json.dumps({"ts": "bad-stamp", "checkpoint": "virtnbdbackup.x"}),
        json.dumps({"ts": "20260105T120000", "checkpoint": ""}),
        json.dumps({"ts": "20260110T120000", "checkpoint": "virtnbdbackup.2"}),
    ]
    (chain_dir / RUNS_FILE).write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert select_checkpoint(chain_dir, _at(2026, 1, 7)).checkpoint == "virtnbdbackup.0"
    assert select_checkpoint(chain_dir, _at(2026, 1, 9)).checkpoint == "virtnbdbackup.0"
