from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system import backup_usage, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import BackupRow
from libvirt_backup_system.shell import CommandError, CommandResult

ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BETA_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": host_id,
        }
    )
    return cfg


def _peer(tmp_path: Path, host_id: str) -> kopia_repo.PeerRepo:
    return kopia_repo.PeerRepo(
        host_id=host_id,
        repo_path=tmp_path / "backups" / host_id / "kopia-repo",
        config_file=tmp_path / f"{host_id}.config",
    )


def test_du_top_level_reports_repo_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cfg = _make_config(tmp_path)
    peers = [_peer(tmp_path, "host-a"), _peer(tmp_path, "host-b")]
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: peers)
    monkeypatch.setattr(backup_usage, "_repo_bytes", lambda path: 100 if "host-a" in str(path) else 250)

    assert backup_usage.backup_usage(cfg) == 0

    out = capsys.readouterr().out
    assert "host-a" in out
    assert "host-b" in out
    assert "TOTAL" in out
    assert "350" in out


def test_du_top_level_handles_empty_and_discovery_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: [])
    assert backup_usage.backup_usage(cfg) == 0
    assert "no backup repositories found" in capsys.readouterr().out

    def fail_discovery(_config):
        raise kopia_repo.PeerDiscoveryError("denied")

    monkeypatch.setattr(kopia_repo, "discover_peer_repos", fail_discovery)
    assert backup_usage.backup_usage(cfg) == 1


def test_du_top_level_json_is_machine_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: [_peer(tmp_path, "host-a")])
    monkeypatch.setattr(backup_usage, "_repo_bytes", lambda _path: 4096)

    assert backup_usage.backup_usage(cfg, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "hosts"
    assert payload["total_repo_bytes"] == 4096
    assert payload["hosts"][0]["host_id"] == "host-a"


def test_du_rejects_bad_or_missing_host_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    assert backup_usage.backup_usage(cfg, host_id="../bad") == 1
    assert "backup usage host_id rejected" in capsys.readouterr().err

    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: [_peer(tmp_path, "host-a")])
    assert backup_usage.backup_usage(cfg, host_id="host-b") == 1
    assert "backup host repo not found" in capsys.readouterr().err


def test_du_vm_rows_join_names_and_report_latest_logical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    peers = [_peer(tmp_path, "host-a"), _peer(tmp_path, "host-b")]
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: peers)
    monkeypatch.setattr(backup_usage, "_connect_repo", lambda _config, host_id: tmp_path / f"{host_id}.config")
    monkeypatch.setattr(
        backup_usage,
        "rows_from_repo",
        lambda _config, *, host_id, config_file: [
            BackupRow(ALPHA_UUID, "20260101T000000", host_id, "alpha", "run-1", "meta-1", config_file),
            BackupRow(ALPHA_UUID, "20260102T000000", host_id, "alpha", "run-2", "meta-2", config_file),
            BackupRow(ALPHA_UUID, "20251231T000000", host_id, "alpha", "run-old", "meta-old", config_file),
            BackupRow(BETA_UUID, "20260101T000000", host_id, "beta", "run-3", "meta-3", config_file),
        ],
    )

    def fake_run_kopia(args, **_kwargs):
        host_id = "host-a" if "host-a.config" in args[1] else "host-b"
        payload = [
            _disk_record(host_id, ALPHA_UUID, "run-1", 100),
            _disk_record(host_id, ALPHA_UUID, "run-2", 125),
            _disk_record(host_id, ALPHA_UUID, "run-2", 25),
            _disk_record(host_id, BETA_UUID, "run-3", 999),
        ]
        return CommandResult(args=args, returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(backup_usage, "run_kopia", fake_run_kopia)

    assert backup_usage.backup_usage(cfg, host_id="host-a", vm_uuid=ALPHA_UUID) == 0

    out = capsys.readouterr().out
    assert ALPHA_UUID in out
    assert BETA_UUID not in out
    assert "alpha" in out
    assert "restore-points" in out
    assert "backup-size" in out
    assert "backup-bytes" not in out
    assert "150" in out
    assert "250" in out
    assert "225" not in out


def test_du_vm_rows_reports_connect_and_list_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    peers = [_peer(tmp_path, "host-a"), _peer(tmp_path, "host-b")]
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: peers)
    monkeypatch.setattr(
        backup_usage,
        "_connect_repo",
        lambda _config, host_id: None if host_id == "host-a" else tmp_path,
    )
    monkeypatch.setattr(backup_usage, "rows_from_repo", lambda *_args, **_kwargs: [])

    def fail_run_kopia(args, **_kwargs):
        raise CommandError(CommandResult(args=args, returncode=1, stdout="", stderr="denied"))

    monkeypatch.setattr(backup_usage, "run_kopia", fail_run_kopia)

    assert backup_usage.backup_usage(cfg, vm_uuid=ALPHA_UUID) == 1
    captured = capsys.readouterr()
    assert "kopia disk usage list failed" in captured.err
    assert "no matching backup usage found" in captured.out


def test_du_vm_json_reports_filtered_logical_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _config: [_peer(tmp_path, "host-a")])
    monkeypatch.setattr(backup_usage, "_connect_repo", lambda _config, _host_id: tmp_path / "host-a.config")
    monkeypatch.setattr(backup_usage, "rows_from_repo", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        backup_usage,
        "run_kopia",
        lambda args, **_kwargs: CommandResult(
            args=args,
            returncode=0,
            stdout=json.dumps([_disk_record("host-a", BETA_UUID, "run-1", 512)]),
            stderr="",
        ),
    )

    assert backup_usage.backup_usage(cfg, vm_uuid=BETA_UUID, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "vms"
    assert payload["total_backup_bytes"] == 512
    assert payload["total_latest_logical_bytes"] == 512
    assert payload["vms"][0]["vm_uuid"] == BETA_UUID
    assert payload["vms"][0]["backup_bytes"] == 512
    assert payload["vms"][0]["latest_logical_bytes"] == 512
    assert payload["vms"][0]["restore_point_count"] == 1


def test_repo_bytes_and_human_sizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "one").write_text("abc", encoding="utf-8")
    (repo / "gone").write_text("abc", encoding="utf-8")
    real_entry_size = backup_usage._entry_size

    def flaky_size(path: Path) -> int:
        if path.name == "gone":
            raise FileNotFoundError
        return real_entry_size(path)

    monkeypatch.setattr(backup_usage, "_entry_size", flaky_size)
    assert backup_usage._repo_bytes(repo) >= 0
    assert backup_usage._human_bytes(1) == "1 B"
    assert backup_usage._human_bytes(1024) == "1.0 KiB"
    assert backup_usage._human_bytes(1024**5) == "1.0 PiB"


def test_connect_repo_uses_local_or_peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path, host_id="local")
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _config: tmp_path / "local.config")
    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", lambda _config, host_id: tmp_path / f"{host_id}.config")

    assert backup_usage._connect_repo(cfg, "local") == tmp_path / "local.config"
    assert backup_usage._connect_repo(cfg, "peer") == tmp_path / "peer.config"


def test_disk_usage_from_repo_filters_bad_snapshot_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    payload = [
        "not-object",
        _disk_record("other", ALPHA_UUID, "run-1", 1),
        {"tags": {"tag:host": "host-a", "tag:kind": "meta"}},
        {"tags": {"tag:host": "host-a", "tag:kind": "disk", "tag:vm-uuid": ALPHA_UUID}},
        _disk_record("host-a", BETA_UUID, "run-2", 2),
        _disk_record("host-a", ALPHA_UUID, "run-3", 3),
        {"tags": {"tag:host": "host-a", "tag:kind": "disk", "tag:vm-uuid": ALPHA_UUID, "tag:run-id": "run-4"}},
    ]
    monkeypatch.setattr(
        backup_usage,
        "run_kopia",
        lambda args, **_kwargs: CommandResult(args=args, returncode=0, stdout=json.dumps(payload), stderr=""),
    )

    rows = backup_usage._disk_usage_from_repo(cfg, host_id="host-a", config_file=tmp_path / "cfg", vm_uuid=ALPHA_UUID)

    assert rows == [(ALPHA_UUID, "run-3", 3, 3), (ALPHA_UUID, "run-4", 0, 0)]


def test_disk_usage_rejects_non_array_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _make_config(tmp_path)
    monkeypatch.setattr(
        backup_usage,
        "run_kopia",
        lambda args, **_kwargs: CommandResult(args=args, returncode=0, stdout="{}", stderr=""),
    )

    with pytest.raises(ValueError, match="non-array"):
        backup_usage._disk_usage_from_repo(cfg, host_id="host-a", config_file=tmp_path / "cfg", vm_uuid=None)


def _disk_record(host_id: str, vm_uuid: str, run_id: str, size: int) -> dict[str, object]:
    return {
        "id": f"{host_id}-{run_id}",
        "stats": {"totalSize": size},
        "storageStats": {"newData": {"packedContentBytes": size}},
        "tags": {
            "tag:host": host_id,
            "tag:kind": "disk",
            "tag:run-id": run_id,
            "tag:vm-uuid": vm_uuid,
        },
    }
