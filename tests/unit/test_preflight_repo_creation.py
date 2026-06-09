from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from libvirt_backup_system import preflight
from tests.unit._preflight_helpers import make_config, write_password_file


def test_check_allows_bootstrap_before_backup_path_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    write_password_file(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "local_repo_exists", lambda _cfg: False)
    assert preflight._validate_local_kopia_repo(cfg, require_existing=True) == []


def test_run_preflight_requires_existing_local_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "local_repo_exists", lambda _cfg: False)
    failures = preflight._validate_local_kopia_repo(cfg, require_existing=True)
    assert failures == [preflight.LOCAL_KOPIA_REPO_MISSING_FAILURE]


def test_run_preflight_guides_join_when_peer_repo_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    peer_repo = cfg.path_value("BACKUP_PATH") / "host-b" / preflight.kopia_repo.REPO_DIR_NAME
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("repo\n", encoding="utf-8")
    write_password_file(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "local_repo_exists", lambda _cfg: False)
    failures = preflight._validate_local_kopia_repo(cfg, require_existing=True)
    assert failures == [preflight.LOCAL_KOPIA_REPO_JOIN_FAILURE]


def test_existing_peer_repos_empty_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert preflight._existing_peer_repos(cfg) == []


def test_existing_peer_repos_empty_on_discovery_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(_cfg: object) -> NoReturn:
        raise preflight.kopia_repo.PeerDiscoveryError("cannot scan")

    monkeypatch.setattr(preflight.kopia_repo, "discover_peer_repos", boom)
    assert preflight._existing_peer_repos(cfg) == []


def test_repo_creation_preflight_enforces_required_mount(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    write_password_file(cfg)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    failures = preflight.repo_creation_failures(cfg)
    assert "BACKUP_PATH must be a mount point when BACKUP_REQUIRE_NFS_MOUNT=true" in failures


def test_repo_creation_preflight_rejects_unsafe_host_id(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, host_id="../elsewhere")
    write_password_file(cfg)
    failures = preflight.repo_creation_failures(cfg)
    assert "HOST_ID must not contain path separators or be '.'/'..'" in failures


def test_repo_creation_preflight_guides_join_when_peer_token_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    peer_repo = cfg.path_value("BACKUP_PATH") / "host-b" / preflight.kopia_repo.REPO_DIR_NAME
    peer_repo.mkdir(parents=True)
    (peer_repo / "kopia.repository.f").write_text("repo\n", encoding="utf-8")
    write_password_file(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "ensure_peer_connected", lambda _cfg, _host_id: None)
    failures = preflight.peer_repo_access_failures(cfg)
    assert any("existing peer kopia repo host-b" in failure and "add-node" in failure for failure in failures)


def test_peer_repo_access_failures_empty_when_backup_path_empty(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_PATH"] = ""
    assert preflight.peer_repo_access_failures(cfg) == []


def test_peer_repo_access_failures_reports_discovery_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(_cfg: object) -> NoReturn:
        raise preflight.kopia_repo.PeerDiscoveryError("cannot scan")

    monkeypatch.setattr(preflight.kopia_repo, "discover_peer_repos", boom)
    assert preflight.peer_repo_access_failures(cfg) == ["cannot scan"]


def test_peer_repo_access_failures_skips_local_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    peer = preflight.kopia_repo.PeerRepo("host-a", tmp_path / "repo", tmp_path / "config")
    monkeypatch.setattr(preflight.kopia_repo, "discover_peer_repos", lambda _cfg: [peer])
    monkeypatch.setattr(
        preflight.kopia_repo,
        "ensure_peer_connected",
        lambda _cfg, _host_id: pytest.fail("local host must be skipped"),
    )
    assert preflight.peer_repo_access_failures(cfg) == []


@pytest.mark.parametrize(
    "key", ["KEEP_LATEST", "KEEP_HOURLY", "KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY", "KEEP_ANNUAL"]
)
def test_retention_keys_must_be_non_negative_integers(tmp_path: Path, key: str) -> None:
    cfg = make_config(tmp_path)
    cfg.values[key] = "abc"
    assert f"{key} must be an integer" in preflight._validate_integers(cfg)
    cfg.values[key] = "-1"
    assert f"{key} must be greater than or equal to 0" in preflight._validate_integers(cfg)
