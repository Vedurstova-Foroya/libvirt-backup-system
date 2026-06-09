from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import kopia_repo, preflight
from libvirt_backup_system.config import Config
from tests.unit._preflight_helpers import make_config


def _create_repo_sentinel(config: Config) -> Path:
    repo_path = kopia_repo.local_repo_path(config)
    repo_path.mkdir(parents=True)
    (repo_path / "kopia.repository.f").write_text("sentinel", encoding="utf-8")
    return repo_path


def test_validate_local_kopia_repo_probes_existing_repo_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    repo_path = _create_repo_sentinel(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_connected", lambda _cfg: tmp_path / "kopia.config")

    assert preflight._validate_local_kopia_repo(cfg) == []
    assert [path.name for path in repo_path.iterdir()] == ["kopia.repository.f"]


def test_validate_local_kopia_repo_reports_repo_write_probe_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    repo_path = _create_repo_sentinel(cfg)
    probed: list[Path] = []
    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_connected", lambda _cfg: tmp_path / "kopia.config")

    def fail_probe(path: Path) -> None:
        probed.append(path)
        raise OSError("read-only repo")

    monkeypatch.setattr(preflight, "write_probe", fail_probe)
    failures = preflight._validate_local_kopia_repo(cfg)

    assert failures == ["local kopia repo must be writable: read-only repo"]
    assert probed and probed[0].parent == repo_path


def test_validate_local_kopia_repo_skips_probe_when_repo_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(preflight, "write_probe", lambda _path: pytest.fail("probe should be skipped"))
    assert preflight._validate_local_kopia_repo(cfg) == []


def test_validate_local_kopia_repo_requires_existing_repo_when_locked(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    failures = preflight._validate_local_kopia_repo(cfg, require_existing=True)
    assert failures == [preflight.LOCAL_KOPIA_REPO_MISSING_FAILURE]


def test_validate_local_kopia_repo_reports_existing_repo_connect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    _create_repo_sentinel(cfg)
    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_connected", lambda _cfg: None)

    failures = preflight._validate_local_kopia_repo(cfg, require_existing=True)

    assert failures == [preflight.LOCAL_KOPIA_REPO_CONNECT_FAILURE]


def test_validate_local_kopia_repo_writable_reports_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ValueError from ``local_repo_path`` surfaces as a "path rejected" failure.

    The probe path is reached only after ``local_repo_exists`` /
    ``ensure_local_connected`` succeed, so the cleanest way to exercise the
    handler is to call ``_validate_local_kopia_repo_writable`` directly with
    ``local_repo_path`` monkeypatched to raise.
    """
    cfg = make_config(tmp_path)

    def boom(_cfg: Config) -> Path:
        raise ValueError("KOPIA_REPO_PATH must be an absolute path")

    monkeypatch.setattr(preflight.kopia_repo, "local_repo_path", boom)
    failures = preflight._validate_local_kopia_repo_writable(cfg)
    assert failures == ["local kopia repo path rejected: KOPIA_REPO_PATH must be an absolute path"]
