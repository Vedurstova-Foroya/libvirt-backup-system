from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.run_records import CheckpointReadError, list_checkpoints, record_run


def _write_checkpoint(chain_dir: Path, name: str) -> None:
    (chain_dir / f"{name}.checkpoint").write_text("payload\n", encoding="utf-8")


def test_list_checkpoints_xml_dir_iter_failure_raises(tmp_path: Path, monkeypatch) -> None:
    # OSError on the XML dir is NOT a legitimate "no checkpoints" signal —
    # falling through to legacy would let record_run treat the chain as having
    # no new checkpoint, producing a run restore --at later cannot resolve.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    real_iterdir = Path.iterdir

    def boom(self: Path) -> object:
        if self == chain_dir / "checkpoints":
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", boom)
    with pytest.raises(CheckpointReadError):
        list_checkpoints(chain_dir, "alpha")


def test_list_checkpoints_xml_dir_disappears_returns_none(tmp_path: Path, monkeypatch) -> None:
    # is_dir() returned True (TOCTOU window) but the dir was unlinked before
    # iterdir(). FileNotFoundError is benign — fall through to legacy.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    real_iterdir = Path.iterdir

    def boom(self: Path) -> object:
        if self == chain_dir / "checkpoints":
            raise FileNotFoundError("vanished mid-read")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", boom)
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}


def test_list_checkpoints_legacy_iter_raises_on_oserror(tmp_path: Path, monkeypatch) -> None:
    # A real OSError on the legacy chain-dir listing is also a "cannot tell"
    # state — must propagate CheckpointReadError so the run fails.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    real_iterdir = Path.iterdir

    def boom(self: Path) -> object:
        if self == chain_dir:
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", boom)
    with pytest.raises(CheckpointReadError):
        list_checkpoints(chain_dir, "alpha")


def test_record_run_fails_when_checkpoint_metadata_unreadable(tmp_path: Path, monkeypatch, capsys) -> None:
    # A successful virtnbdbackup followed by an unreadable .cpt would otherwise
    # return True ("run recorded no new checkpoint") and restore --at would
    # refuse weeks later with "no record found." record_run must surface a
    # distinct error and fail the run at backup time.
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    cpt_path = chain_dir / "alpha.cpt"
    cpt_path.write_text(json.dumps(["virtnbdbackup.0"]), encoding="utf-8")
    real_read_text = Path.read_text

    def boom(self: Path, *args: object, **kwargs: object) -> str:
        if self == cpt_path:
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    assert record_run(chain_dir, "20260105T120000", set(), "alpha") is False
    assert "checkpoint metadata read failed" in capsys.readouterr().err
