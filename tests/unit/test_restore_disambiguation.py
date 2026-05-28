from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import restore

from .conftest import ALPHA_UUID
from .restore_helpers import TIMESTAMP, make_config, make_row


def test_match_row_accepts_host_id_disambiguator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    rows = [make_row(tmp_path, host_id="host-a"), make_row(tmp_path, host_id="host-b")]
    monkeypatch.setattr(restore, "enumerate_backups", lambda _cfg, *, vm_uuid=None: rows)
    assert restore._match_row(cfg, ALPHA_UUID, TIMESTAMP, "host-b", None) == rows[1]


def test_match_row_accepts_run_id_disambiguator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    first = make_row(tmp_path, host_id="host-a")
    second = make_row(tmp_path, host_id="host-a", run_id="run-2")
    rows = [first, second]
    monkeypatch.setattr(restore, "enumerate_backups", lambda _cfg, *, vm_uuid=None: rows)
    assert restore._match_row(cfg, ALPHA_UUID, TIMESTAMP, None, "run-2") == second


def test_match_row_rejects_ambiguous_timestamp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    rows = [make_row(tmp_path, host_id="host-a"), make_row(tmp_path, host_id="host-b")]
    monkeypatch.setattr(restore, "enumerate_backups", lambda _cfg, *, vm_uuid=None: rows)
    assert restore._match_row(cfg, ALPHA_UUID, TIMESTAMP, None, None) is None
