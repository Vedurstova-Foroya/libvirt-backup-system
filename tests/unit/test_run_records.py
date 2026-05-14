from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from libvirt_backup_system.run_records import (
    RUNS_FILE,
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


def test_record_run_writes_jsonl_for_each_new_checkpoint(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    before = list_checkpoints(chain_dir)
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    record_run(chain_dir, "20260105T120000", before)
    lines = (chain_dir / RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"checkpoint": "virtnbdbackup.1", "ts": "20260105T120000"},
    ]


def test_record_run_skips_when_no_new_checkpoint(tmp_path: Path, capsys) -> None:
    # Defensive: an anomalous run that did not add a checkpoint must not write
    # a half-empty record; select_checkpoint will fall back to chain-end.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    before = list_checkpoints(chain_dir)
    record_run(chain_dir, "20260105T120000", before)
    assert not (chain_dir / RUNS_FILE).exists()
    assert "run recorded no new checkpoint" in capsys.readouterr().out


def test_record_run_appends_to_existing_jsonl(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    record_run(chain_dir, "20260101T080000", set())
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    record_run(chain_dir, "20260102T080000", {"virtnbdbackup.0"})
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
    assert select_checkpoint(chain_dir, _at(2026, 1, 7, 12)) == "virtnbdbackup.1"
    # Exact match wins.
    assert select_checkpoint(chain_dir, _at(2026, 1, 5, 12)) == "virtnbdbackup.1"
    # Older than every record falls through to None even though caller should
    # have picked an earlier chain — this is a defensive fallback, not a
    # primary path.
    assert select_checkpoint(chain_dir, _at(2026, 1, 1, 7)) is None


def test_select_checkpoint_returns_none_at_or_after_last_run(tmp_path: Path) -> None:
    # at >= latest recorded run means "restore the chain end" — omit --until
    # so virtnbdrestore replays everything in the chain dir.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"}) + "\n",
        encoding="utf-8",
    )
    assert select_checkpoint(chain_dir, _at(2026, 1, 1, 8)) is None
    assert select_checkpoint(chain_dir, _at(2026, 6, 1)) is None


def test_select_checkpoint_returns_none_for_legacy_chain(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    # No runs.jsonl at all — legacy chain layout.
    assert select_checkpoint(chain_dir, _at(2026, 1, 1)) is None


def test_record_run_logs_when_open_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    # If runs.jsonl cannot be opened for append (read-only filesystem, ENOSPC),
    # surface the failure as a logged error rather than crashing the backup run.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")

    def fail(self: Path, *args: object, **kwargs: object) -> object:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "open", fail)
    record_run(chain_dir, "20260105T120000", set())
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
    assert select_checkpoint(chain_dir, _at(2026, 1, 3)) == "virtnbdbackup.0"


def test_select_checkpoint_skips_corrupt_lines(tmp_path: Path) -> None:
    # A truncated power-loss line or hand-edit must not poison the rest of
    # the file: surviving records still drive the right --until pick.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / RUNS_FILE).write_text(
        "\n".join(
            [
                json.dumps({"ts": "20260101T080000", "checkpoint": "virtnbdbackup.0"}),
                "not-json{",
                json.dumps({"ts": "bad-stamp", "checkpoint": "virtnbdbackup.x"}),
                json.dumps({"ts": "20260105T120000", "checkpoint": ""}),
                json.dumps({"ts": "20260110T120000", "checkpoint": "virtnbdbackup.2"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert select_checkpoint(chain_dir, _at(2026, 1, 7)) == "virtnbdbackup.0"
    assert select_checkpoint(chain_dir, _at(2026, 1, 9)) == "virtnbdbackup.0"
