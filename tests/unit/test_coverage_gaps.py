"""Tests covering the remaining gaps in cli, kopia_snapshots, list_restore_points,
preflight, and preflight_estimate.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from libvirt_backup_system import (
    kopia_repo,
    kopia_snapshots,
    list_restore_points,
    preflight,
    preflight_estimate,
)
from libvirt_backup_system.cli import _list_restore_points_command
from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import BackupEnumeration, BackupRow
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit._preflight_helpers import make_config

ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RUN_ID_A = "11111111-1111-1111-1111-111111111111"


def _make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "HOST_ID": host_id,
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
        }
    )
    (tmp_path / "backups").mkdir(parents=True, exist_ok=True)
    return cfg


def _row(tmp_path: Path) -> BackupRow:
    return BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp="20260521T023001",
        host_id="host-a",
        vm_name="alpha",
        run_id=RUN_ID_A,
        snapshot_id="snap-1",
        config_file=tmp_path / "k.config",
    )


# ---------------------------------------------------------------------------
# cli.py: _list_restore_points_command json_output=True paths
# ---------------------------------------------------------------------------


def test_cli_list_restore_points_json_returns_config_code_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line 74: return config_code when validate_config fails in json mode."""
    cfg = _make_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _cfg: 1)
    assert _list_restore_points_command(cfg, json_output=True) == 1


def test_cli_list_restore_points_json_returns_one_when_backup_path_not_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line 76: return 1 when runtime_backup_path_ok fails in json mode."""
    cfg = _make_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _cfg: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.runtime_backup_path_ok", lambda _cfg: False)
    assert _list_restore_points_command(cfg, json_output=True) == 1


def test_cli_list_restore_points_json_returns_one_when_enumeration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Line 79: return 1 when enumerate_backups_result.ok is False in json mode."""
    cfg = _make_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _cfg: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.enumerate_backups_result",
        lambda _cfg: BackupEnumeration([], ok=False),
    )
    assert _list_restore_points_command(cfg, json_output=True) == 1


def test_cli_list_restore_points_json_prints_json_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Line 80: print(format_json(result.rows)) when all checks pass in json mode."""
    cfg = _make_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda _cfg: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.runtime_backup_path_ok", lambda _cfg: True)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.enumerate_backups_result",
        lambda _cfg: BackupEnumeration([_row(tmp_path)], ok=True),
    )
    assert _list_restore_points_command(cfg, json_output=True) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload[0]["vm_uuid"] == ALPHA_UUID


# ---------------------------------------------------------------------------
# kopia_snapshots.py: _snapshot_id_from_create_stdout error paths
# ---------------------------------------------------------------------------


def test_snapshot_id_from_create_stdout_raises_on_non_json() -> None:
    """Lines 200-201: JSONDecodeError -> ValueError."""
    with pytest.raises(ValueError, match="did not return JSON"):
        kopia_snapshots._snapshot_id_from_create_stdout("not-json")


def test_snapshot_id_from_create_stdout_raises_on_missing_id() -> None:
    """Line 205: valid JSON but no id field."""
    with pytest.raises(ValueError, match="did not include id"):
        kopia_snapshots._snapshot_id_from_create_stdout("{}")


def test_snapshot_id_from_create_stdout_raises_on_empty_id() -> None:
    """Line 205: id present but empty string."""
    with pytest.raises(ValueError, match="did not include id"):
        kopia_snapshots._snapshot_id_from_create_stdout('{"id": ""}')


def test_snapshot_id_from_create_stdout_raises_on_non_string_id() -> None:
    """Line 205: id present but not a string."""
    with pytest.raises(ValueError, match="did not include id"):
        kopia_snapshots._snapshot_id_from_create_stdout('{"id": 123}')


# ---------------------------------------------------------------------------
# list_restore_points.py: _peer_rows_result PeerDiscoveryError
# ---------------------------------------------------------------------------


def test_peer_rows_result_returns_not_ok_on_discovery_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lines 61-62: PeerDiscoveryError -> BackupEnumeration([], ok=False)."""
    cfg = _make_config(tmp_path)

    def boom(_cfg: Config) -> Any:
        raise kopia_repo.PeerDiscoveryError("boom")

    monkeypatch.setattr(kopia_repo, "discover_peer_repos", boom)
    result = list_restore_points._peer_rows_result(cfg)
    assert result.rows == []
    assert result.ok is False


# ---------------------------------------------------------------------------
# preflight.py: _validate_local_kopia_repo with empty BACKUP_PATH
# ---------------------------------------------------------------------------


def test_validate_local_kopia_repo_returns_empty_when_backup_path_empty(tmp_path: Path) -> None:
    """Line 151: return [] when BACKUP_PATH is empty."""
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert preflight._validate_local_kopia_repo(cfg) == []


# ---------------------------------------------------------------------------
# preflight_estimate.py: uncovered branches
# ---------------------------------------------------------------------------


def test_disk_image_info_rejects_non_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 29: raise ValueError when qemu-img returns a non-dict JSON."""
    monkeypatch.setattr(
        preflight_estimate, "run", lambda args, **_: CommandResult(args, 0, '"just a string"', "")
    )
    with pytest.raises(ValueError, match="did not return a JSON object"):
        preflight_estimate.disk_image_info("/tmp/x.qcow2")


def test_disk_virtual_size_bytes_rejects_unexpected_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 37: raise TypeError when virtual-size is not int/float/str."""
    monkeypatch.setattr(
        preflight_estimate,
        "disk_image_info",
        lambda path: {"virtual-size": [1, 2, 3]},
    )
    with pytest.raises(TypeError, match="unexpected virtual-size type"):
        preflight_estimate.disk_virtual_size_bytes("/tmp/x.qcow2")


def _cfg(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "b"),
            "HOST_ID": "h",
            "BACKUP_ESTIMATE_GB_PER_VM": "1",
            "BACKUP_INCREMENTAL_MULTIPLIER": "1.2",
            "SPACE_MARGIN_PERCENT": "20",
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    return cfg


def test_estimate_required_kb_returns_zero_on_bad_int_value(tmp_path: Path) -> None:
    """Lines 69-70: ValueError from int_value(SPACE_MARGIN_PERCENT) -> return 0."""
    cfg = _cfg(tmp_path)
    cfg.values["SPACE_MARGIN_PERCENT"] = "not-an-int"
    vm = VM("a", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    monkeypatch_needed = False
    # _vms_needing_first_backup_estimate returns the vm list when repo does not exist
    assert preflight_estimate.estimate_required_kb(cfg, [vm]) == 0


def test_estimate_required_kb_returns_zero_on_nonfinite_fallback(tmp_path: Path) -> None:
    """Line 72: non-finite fallback_per_vm_gb -> return 0."""
    cfg = _cfg(tmp_path)
    cfg.values["BACKUP_ESTIMATE_GB_PER_VM"] = "inf"
    vm = VM("a", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert preflight_estimate.estimate_required_kb(cfg, [vm]) == 0


def test_vms_needing_first_backup_estimate_returns_empty_when_connect_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Line 84: ensure_local_connected returns None -> return []."""
    cfg = _cfg(tmp_path)
    vm = VM("a", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    monkeypatch.setattr(preflight_estimate.kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(preflight_estimate.kopia_repo, "ensure_local_connected", lambda _cfg: None)
    result = preflight_estimate._vms_needing_first_backup_estimate(cfg, [vm])
    assert result == []


def test_vms_needing_first_backup_estimate_returns_empty_on_command_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lines 92-94: snapshot_list raises CommandError -> return []."""
    cfg = _cfg(tmp_path)
    vm = VM("a", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    monkeypatch.setattr(preflight_estimate.kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(
        preflight_estimate.kopia_repo, "ensure_local_connected", lambda _cfg: tmp_path / "kopia.config"
    )
    monkeypatch.setattr(preflight_estimate.kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(preflight_estimate.kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")

    def boom(**_: Any) -> Any:
        raise CommandError(CommandResult(["kopia"], 1, "", "denied"))

    monkeypatch.setattr(preflight_estimate.kopia_snapshots, "snapshot_list", boom)
    result = preflight_estimate._vms_needing_first_backup_estimate(cfg, [vm])
    assert result == []


def test_vms_needing_first_backup_estimate_returns_empty_on_value_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Lines 92-94: snapshot_list raises ValueError -> return []."""
    cfg = _cfg(tmp_path)
    vm = VM("a", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    monkeypatch.setattr(preflight_estimate.kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(
        preflight_estimate.kopia_repo, "ensure_local_connected", lambda _cfg: tmp_path / "kopia.config"
    )
    monkeypatch.setattr(preflight_estimate.kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(preflight_estimate.kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")

    def boom(**_: Any) -> Any:
        raise ValueError("bad data")

    monkeypatch.setattr(preflight_estimate.kopia_snapshots, "snapshot_list", boom)
    result = preflight_estimate._vms_needing_first_backup_estimate(cfg, [vm])
    assert result == []
