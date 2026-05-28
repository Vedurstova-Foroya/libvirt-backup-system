from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_snapshots
from libvirt_backup_system.shell import CommandError, CommandResult

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _write_password(path: Path, value: str = "swordfish") -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _make_run_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> list[tuple[list[str], Mapping[str, str] | None]]:
    captured: list[tuple[list[str], Mapping[str, str] | None]] = []

    def fake_run(
        args: list[str], *, check: bool = True, env: Mapping[str, str] | None = None, **_: Any
    ) -> CommandResult:
        captured.append((args, env))
        if returncode != 0 and check:
            raise CommandError(CommandResult(args, returncode, stdout, stderr))
        return CommandResult(args, returncode, stdout, stderr)

    monkeypatch.setattr(kopia_client, "run", fake_run)
    monkeypatch.setattr(kopia_client, "run_streamed", fake_run)
    return captured


def test_snapshot_list_parses_fixture_and_filters_by_tags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    body = (FIXTURE_DIR / "kopia_snapshot_list.json").read_text(encoding="utf-8")
    _make_run_capture(monkeypatch, stdout=body)
    snapshots = kopia_snapshots.snapshot_list(
        config_file=tmp_path / "c",
        password_file=password,
        tags={"kind": "meta"},
    )
    assert len(snapshots) == 1
    snap = snapshots[0]
    assert snap.snapshot_id == "0123456789abcdef"
    assert snap.source_host == "lbs-host-a"
    assert snap.source_user == "root"
    assert snap.source.startswith("root@lbs-host-a")
    assert snap.tags["vm-uuid"] == "00000000-0000-0000-0000-000000000001"


def test_snapshot_list_skips_records_without_required_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    payload = json.dumps(
        [
            {"id": 7, "source": {"host": "h", "path": "p"}, "tags": {}},
            {"id": "abc", "source": {"host": "h"}, "tags": {}},
            {"id": "ok", "source": {"host": "h", "path": "p", "userName": 9}, "tags": {"a": 1, "b": "ok"}},
            "garbage",
        ]
    )
    _make_run_capture(monkeypatch, stdout=payload)
    snapshots = kopia_snapshots.snapshot_list(config_file=tmp_path / "c", password_file=password)
    assert [s.snapshot_id for s in snapshots] == ["ok"]
    assert snapshots[0].tags == {"b": "ok"}
    assert snapshots[0].source_user == ""


def test_snapshot_list_rejects_non_array_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    _make_run_capture(monkeypatch, stdout="{}")
    with pytest.raises(ValueError, match="non-array"):
        kopia_snapshots.snapshot_list(config_file=tmp_path / "c", password_file=password)


def test_snapshot_list_rejects_unparseable_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    _make_run_capture(monkeypatch, stdout="not-json")
    with pytest.raises(ValueError, match="unparseable JSON"):
        kopia_snapshots.snapshot_list(config_file=tmp_path / "c", password_file=password)


def test_snapshot_create_path_passes_tags_and_override_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_create_path(
        config_file=tmp_path / "c",
        password_file=password,
        path=tmp_path / "manifest",
        tags={"vm-uuid": "uuid-x", "kind": "meta"},
        override_source="host-a:libvirt-backup:uuid-x/meta",
        parallelism=4,
    )
    args, _ = captured[0]
    assert "--tags=vm-uuid:uuid-x" in args
    assert "--tags=kind:meta" in args
    assert "--override-source" in args
    assert "host-a:libvirt-backup:uuid-x/meta" in args
    assert "--parallel" in args


def test_snapshot_create_path_omits_optional_args(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_create_path(
        config_file=tmp_path / "c",
        password_file=password,
        path=tmp_path / "manifest",
        tags={},
    )
    args, _ = captured[0]
    assert "--override-source" not in args
    assert "--parallel" not in args


def test_snapshot_verify_includes_supplied_percentage_and_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_verify(
        config_file=tmp_path / "c",
        password_file=password,
        verify_files_percent=1.0,
        snapshot_ids=["abc"],
    )
    args, _ = captured[0]
    assert "--max-errors=0" in args
    assert "--verify-files-percent=1.0" in args
    assert args[-1] == "abc"


def test_snapshot_verify_omits_unset_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_verify(config_file=tmp_path / "c", password_file=password)
    args, _ = captured[0]
    assert all("--verify-files-percent" not in arg for arg in args)
    assert "--dry-run" not in args


def test_snapshot_verify_places_snapshot_ids_after_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_verify(
        config_file=tmp_path / "c",
        password_file=password,
        verify_files_percent=0.0,
        snapshot_ids=["abc"],
    )
    args, _ = captured[0]
    # snapshot IDs are positional and trail every option so kopia does not
    # mistake the next flag for a snapshot id.
    assert args[-1] == "abc"
    assert args.index("--verify-files-percent=0.0") < args.index("abc")


def test_snapshot_restore_to_path_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_restore_to_path(
        config_file=tmp_path / "c",
        password_file=password,
        snapshot_id="abc",
        dest=tmp_path / "out",
    )
    args, _ = captured[0]
    assert args[-1] == str(tmp_path / "out")
    assert "abc" in args


def test_snapshot_delete_confirms_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_snapshots.snapshot_delete(config_file=tmp_path / "c", password_file=password, snapshot_id="snap-1")
    args, _ = captured[0]
    assert args[-3:] == ["delete", "snap-1", "--delete"]
