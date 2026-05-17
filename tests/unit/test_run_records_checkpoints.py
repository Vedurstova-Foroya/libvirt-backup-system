from __future__ import annotations

import json
from pathlib import Path

from libvirt_backup_system.run_records import list_checkpoints


def _write_checkpoint(chain_dir: Path, name: str) -> None:
    cp_dir = chain_dir / "checkpoints"
    cp_dir.mkdir(exist_ok=True)
    (cp_dir / f"{name}.xml").write_text(f"<domaincheckpoint><name>{name}</name></domaincheckpoint>\n", encoding="utf-8")


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
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "alpha.cpt").write_text(
        json.dumps(["virtnbdbackup.0", "virtnbdbackup.1"]),
        encoding="utf-8",
    )
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0", "virtnbdbackup.1"}


def test_list_checkpoints_falls_back_to_checkpoint_xml_dir(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    (chain_dir / "checkpoints" / "virtnbdbackup.0.xml").write_text("<x/>", encoding="utf-8")
    (chain_dir / "checkpoints" / "virtnbdbackup.1.xml").write_text("<x/>", encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0", "virtnbdbackup.1"}


def test_list_checkpoints_falls_through_on_corrupt_cpt_file(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "alpha.cpt").write_text("not-json", encoding="utf-8")
    _write_checkpoint(chain_dir, "virtnbdbackup.0")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}
    (chain_dir / "alpha.cpt").write_text('{"name":"v0"}', encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == {"virtnbdbackup.0"}


def test_list_checkpoints_xml_dir_empty_falls_through(tmp_path: Path) -> None:
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    (chain_dir / "checkpoints").mkdir()
    (chain_dir / "checkpoints" / "stray.txt").write_text("not xml\n", encoding="utf-8")
    assert list_checkpoints(chain_dir, "alpha") == set()
