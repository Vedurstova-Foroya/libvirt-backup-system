"""Tests for ``doctor._check_local_kopia_repo`` and ``doctor._check_peer_kopia_repos``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import doctor, kopia_client, kopia_repo, kopia_snapshots
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit._doctor_helpers import make_config


def _stub_local_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cfg_file: Path) -> None:
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: cfg_file)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")


def test_check_local_kopia_repo_skipped_when_no_backup_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    assert doctor._check_local_kopia_repo(cfg) == []


def test_check_local_kopia_repo_missing_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: False)
    monkeypatch.setattr(kopia_repo, "local_repo_path", lambda _cfg: Path("/missing"))
    failures = doctor._check_local_kopia_repo(cfg)
    assert any("local kopia repo missing" in failure for failure in failures)


def test_check_local_kopia_repo_missing_config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: tmp_path / "absent.config")
    failures = doctor._check_local_kopia_repo(cfg)
    assert any("local kopia config-file missing" in failure for failure in failures)


def test_check_local_kopia_repo_status_command_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: cfg_file)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise CommandError(CommandResult(["kopia"], 1, "", "denied"))

    monkeypatch.setattr(kopia_client, "repository_status", boom)
    failures = doctor._check_local_kopia_repo(cfg)
    assert any("did not connect cleanly" in failure for failure in failures)


def test_check_local_kopia_repo_status_value_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: cfg_file)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ValueError("garbage JSON")

    monkeypatch.setattr(kopia_client, "repository_status", boom)
    failures = doctor._check_local_kopia_repo(cfg)
    assert any("did not connect cleanly" in failure for failure in failures)


def test_check_local_kopia_repo_status_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: cfg_file)
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda _cfg: tmp_path / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda _cfg: tmp_path / "cache")
    monkeypatch.setattr(kopia_client, "repository_status", lambda **_: {"ok": True})
    assert doctor._check_local_kopia_repo(cfg) == []


def test_check_peer_kopia_repos_skips_local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    peer = kopia_repo.PeerRepo(host_id="host-a", repo_path=tmp_path / "r", config_file=tmp_path / "c")
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _cfg: [peer])

    def fail(*_a: Any, **_kw: Any) -> Path:
        pytest.fail("must not call ensure_peer_connected for local host")

    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", fail)
    assert doctor._check_peer_kopia_repos(cfg) == []


def test_check_peer_kopia_repos_reports_failed_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    peers = [
        kopia_repo.PeerRepo("host-b", tmp_path / "rb", tmp_path / "cb"),
        kopia_repo.PeerRepo("host-c", tmp_path / "rc", tmp_path / "cc"),
    ]
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _cfg: peers)

    def selective(_cfg: Config, host_id: str) -> Path | None:
        return None if host_id == "host-c" else (tmp_path / "cb")

    monkeypatch.setattr(kopia_repo, "ensure_peer_connected", selective)
    failures = doctor._check_peer_kopia_repos(cfg)
    assert any("peer kopia repo host-c did not connect" in failure for failure in failures)
    assert all("host-b" not in failure for failure in failures)


def test_check_peer_kopia_repos_returns_empty_when_no_peers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _cfg: [])
    assert doctor._check_peer_kopia_repos(cfg) == []


def test_check_local_kopia_maintenance_dry_run_skipped_when_no_backup_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    assert doctor._check_local_kopia_maintenance_dry_run(cfg) == []


def test_check_local_kopia_maintenance_dry_run_skipped_when_repo_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Doctor already emits a clearer "local kopia repo missing" failure via
    # _check_local_kopia_repo; the dry-run helper must stay quiet so the
    # operator only sees one message for the same root cause.
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: False)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: tmp_path / "absent.config")
    assert doctor._check_local_kopia_maintenance_dry_run(cfg) == []


def test_check_local_kopia_maintenance_dry_run_skipped_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: tmp_path / "absent.config")
    assert doctor._check_local_kopia_maintenance_dry_run(cfg) == []


def test_check_local_kopia_maintenance_dry_run_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)
    captured: list[dict[str, Any]] = []

    def fake_maintenance_run(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(kopia_client, "maintenance_run", fake_maintenance_run)
    assert doctor._check_local_kopia_maintenance_dry_run(cfg) == []
    assert captured[0]["dry_run"] is True


def test_check_local_kopia_maintenance_dry_run_surfaces_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)

    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 2, "", "maintenance broken"))

    monkeypatch.setattr(kopia_client, "maintenance_run", boom)
    failures = doctor._check_local_kopia_maintenance_dry_run(cfg)
    assert any("maintenance dry-run failed" in failure for failure in failures)
    assert any("maintenance broken" in failure for failure in failures)


def test_check_local_kopia_maintenance_dry_run_surfaces_returncode_when_stderr_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When kopia exits non-zero with no stderr (rare but possible — e.g.
    # SIGKILL'd by the OOM killer), the failure message MUST still include
    # the returncode so the operator has something actionable.
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)

    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 137, "", ""))

    monkeypatch.setattr(kopia_client, "maintenance_run", boom)
    failures = doctor._check_local_kopia_maintenance_dry_run(cfg)
    assert any("137" in failure for failure in failures)


def test_check_local_kopia_verify_dry_run_skipped_when_no_backup_path(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    assert doctor._check_local_kopia_verify_dry_run(cfg) == []


def test_check_local_kopia_verify_dry_run_skipped_when_repo_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: False)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: tmp_path / "absent.config")
    assert doctor._check_local_kopia_verify_dry_run(cfg) == []


def test_check_local_kopia_verify_dry_run_skipped_when_config_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda _cfg: tmp_path / "absent.config")
    assert doctor._check_local_kopia_verify_dry_run(cfg) == []


def test_check_local_kopia_verify_dry_run_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)
    captured: list[dict[str, Any]] = []

    def fake_verify(**kwargs: Any) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", fake_verify)
    assert doctor._check_local_kopia_verify_dry_run(cfg) == []
    assert captured[0]["dry_run"] is True


def test_check_local_kopia_verify_dry_run_surfaces_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)

    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 3, "", "verify broken"))

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", boom)
    failures = doctor._check_local_kopia_verify_dry_run(cfg)
    assert any("verify dry-run failed" in failure for failure in failures)
    assert any("verify broken" in failure for failure in failures)


def test_check_local_kopia_verify_dry_run_surfaces_returncode_when_stderr_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg_file = tmp_path / "kopia.config"
    cfg_file.write_text("{}", encoding="utf-8")
    _stub_local_repo(monkeypatch, tmp_path, cfg_file)

    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 9, "", ""))

    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", boom)
    failures = doctor._check_local_kopia_verify_dry_run(cfg)
    assert any("9" in failure for failure in failures)
