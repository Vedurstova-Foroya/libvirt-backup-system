"""Coverage for ``restore._match_row`` edge cases.

Kept in its own file so ``test_restore.py`` stays under the project's
300-LOC ceiling.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import restore
from libvirt_backup_system.list_restore_points import BackupRow

from .conftest import ALPHA_UUID
from .restore_helpers import TIMESTAMP, make_config, rows_result


def test_match_row_logs_incomplete_when_partial_peer_failure_misses_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Some peers failed AND no timestamp matched among the rest: log "incomplete"."""
    other = BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp="20250101T010101",
        host_id="host-a",
        vm_name="",
        run_id="r",
        snapshot_id="s",
        config_file=tmp_path / "kopia.config",
    )
    monkeypatch.setattr(
        restore,
        "enumerate_backups_result",
        lambda _c, *, vm_uuid=None: rows_result([other], ok=False, failed_host_ids=("host-b",)),
    )
    assert restore.restore(make_config(tmp_path), ALPHA_UUID, TIMESTAMP) == 1
    assert "restore backup enumeration incomplete" in capsys.readouterr().err
