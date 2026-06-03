from __future__ import annotations

from pathlib import Path

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
    assert failures == ["local kopia repo could not be connected with the shared password"]


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


@pytest.mark.parametrize(
    "key", ["KEEP_LATEST", "KEEP_HOURLY", "KEEP_DAILY", "KEEP_WEEKLY", "KEEP_MONTHLY", "KEEP_ANNUAL"]
)
def test_retention_keys_must_be_non_negative_integers(tmp_path: Path, key: str) -> None:
    cfg = make_config(tmp_path)
    cfg.values[key] = "abc"
    assert f"{key} must be an integer" in preflight._validate_integers(cfg)
    cfg.values[key] = "-1"
    assert f"{key} must be greater than or equal to 0" in preflight._validate_integers(cfg)
