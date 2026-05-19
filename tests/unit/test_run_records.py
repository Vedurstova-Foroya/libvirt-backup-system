from __future__ import annotations

import json
from pathlib import Path

from libvirt_backup_system.run_records import (
    RUNS_FILE,
    chain_is_poisoned,
    list_checkpoints,
    poison_chain,
    record_run,
)


def _write_checkpoint(chain_dir: Path, name: str) -> None:
    cp_dir = chain_dir / "checkpoints"
    cp_dir.mkdir(exist_ok=True)
    (cp_dir / f"{name}.xml").write_text(f"<domaincheckpoint><name>{name}</name></domaincheckpoint>\n", encoding="utf-8")


def test_record_run_writes_jsonl_for_each_new_checkpoint(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    _write_checkpoint(chain_dir, "virtnbdbackup.1")

    assert record_run(chain_dir, "20260101T080000", before=set())
    lines = (chain_dir / RUNS_FILE).read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(line) for line in lines]
    assert {entry["checkpoint"] for entry in parsed} == {"virtnbdbackup.0", "virtnbdbackup.1"}
    assert {entry["ts"] for entry in parsed} == {"20260101T080000"}


def test_record_run_skips_when_no_new_checkpoint(tmp_path: Path, capsys) -> None:
    # If virtnbdbackup did not advance the checkpoint set, nothing to record.
    # The run is still treated as success because chain-end semantics already
    # cover the case in restore.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")

    assert record_run(chain_dir, "20260101T080000", before={"virtnbdbackup.0"})
    assert not (chain_dir / RUNS_FILE).exists()
    assert "run recorded no new checkpoint" in capsys.readouterr().out


def test_record_run_appends_to_existing_jsonl(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert record_run(chain_dir, "20260101T080000", before=set())
    _write_checkpoint(chain_dir, "virtnbdbackup.1")
    assert record_run(chain_dir, "20260102T080000", before={"virtnbdbackup.0"})

    lines = (chain_dir / RUNS_FILE).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    second = json.loads(lines[1])
    assert second["ts"] == "20260102T080000"
    assert second["checkpoint"] == "virtnbdbackup.1"


def test_record_run_swallows_fsync_directory_failure(tmp_path: Path, monkeypatch) -> None:
    # Some NFS configurations refuse to fsync directories. The record itself
    # is still flushed and the run must succeed; only the directory fsync is
    # best-effort.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    real_open = __import__("os").open

    def boom(path: object, flags: int, mode: int = 0o777) -> int:
        if isinstance(path, Path) and path == chain_dir:
            raise OSError("denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr("libvirt_backup_system.run_records.os.open", boom)
    assert record_run(chain_dir, "20260101T080000", before=set())
    assert (chain_dir / RUNS_FILE).exists()


def test_record_run_logs_when_open_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    real_open = Path.open

    def fail(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == RUNS_FILE:
            raise OSError("read-only")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail)
    assert not record_run(chain_dir, "20260101T080000", before=set())
    assert "run record write failed" in capsys.readouterr().err


def test_record_run_fails_when_expect_new_and_no_checkpoint(tmp_path: Path, capsys) -> None:
    # Running-VM backups expect virtnbdbackup to advance the checkpoint.
    # If no new one appeared, refuse to fall through to chain-end behaviour:
    # we cannot prove the chain matches the backup the operator just took.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")

    assert not record_run(chain_dir, "20260101T080000", before={"virtnbdbackup.0"}, expect_new=True)
    assert "expected new checkpoint but none appeared" in capsys.readouterr().err


def test_chain_is_poisoned_fails_closed_on_stat_error(tmp_path: Path, monkeypatch, capsys) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    poison = chain_dir / ".chain-poisoned"
    poison.touch()
    real_lstat = Path.lstat

    def boom(self: Path) -> object:
        if self == poison:
            raise PermissionError("denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", boom)
    assert chain_is_poisoned(chain_dir) is True
    assert "chain poison sentinel check failed" in capsys.readouterr().err


def test_list_checkpoints_handles_missing_sources(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    assert list_checkpoints(chain_dir, "alpha") == set()


def test_poison_chain_writes_sentinel(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    assert poison_chain(chain_dir, "alpha", "reason")
    assert chain_is_poisoned(chain_dir)
