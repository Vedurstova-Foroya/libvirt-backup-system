"""End-to-end ``doctor`` orchestration and helpers (wrapper / package / config).

The unit-level branch coverage of ``_check_units``, ``_check_runtime_state``,
``_check_local_kopia_repo``, and ``_check_peer_kopia_repos`` lives in the
sibling ``test_doctor_*.py`` files so each module stays under the 300-line
project ceiling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import doctor, kopia_client, kopia_repo
from libvirt_backup_system.config import prefixed
from libvirt_backup_system.systemd_units import CHECK_UNIT_NAME, RUN_UNIT_NAME, TIMER_UNIT_NAME
from tests.unit._doctor_helpers import (
    make_config,
    make_install_files,
    stub_preflight,
    stub_systemctl_off,
)


def _stub_repo_local_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda cfg: cfg.prefix / "kopia.config")
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda cfg: cfg.prefix / "cache")
    monkeypatch.setattr(kopia_client, "repository_status", lambda **_: {"ok": True})
    monkeypatch.setattr(kopia_repo, "discover_peer_repos", lambda _cfg: [])


def test_doctor_passes_when_install_and_kopia_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    make_install_files(cfg)
    stub_preflight(monkeypatch)
    stub_systemctl_off(monkeypatch)
    _stub_repo_local_ok(monkeypatch)
    assert doctor.doctor(cfg) == 0


def test_doctor_returns_one_when_preflight_failures_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path, with_backup_path=False)
    make_install_files(cfg)
    stub_preflight(monkeypatch, failures=["pre-existing failure"])
    stub_systemctl_off(monkeypatch)
    _stub_repo_local_ok(monkeypatch)
    assert doctor.doctor(cfg) == 1


def test_check_wrapper_missing(tmp_path: Path) -> None:
    failures = doctor._check_wrapper(tmp_path)
    assert any("wrapper script missing" in failure for failure in failures)


def test_check_wrapper_not_executable(tmp_path: Path) -> None:
    bin_path = prefixed(doctor.WRAPPER_PATH, tmp_path)
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")
    bin_path.chmod(0o644)
    failures = doctor._check_wrapper(tmp_path)
    assert any("not executable" in failure for failure in failures)


def test_check_wrapper_ok(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    make_install_files(cfg)
    assert doctor._check_wrapper(cfg.prefix) == []


def test_check_package_missing(tmp_path: Path) -> None:
    failures = doctor._check_package(tmp_path)
    assert any("package directory missing" in failure for failure in failures)


def test_check_package_present(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    make_install_files(cfg)
    assert doctor._check_package(cfg.prefix) == []


def test_check_config_file_missing(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    failures = doctor._check_config_file(cfg)
    assert any("config file missing" in failure for failure in failures)


def test_check_config_file_present(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    make_install_files(cfg)
    assert doctor._check_config_file(cfg) == []


def test_expected_unit_text_run_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, RUN_UNIT_NAME)
    assert text is not None
    assert "ExecStart" in text


def test_expected_unit_text_check_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, CHECK_UNIT_NAME)
    assert text is not None
    assert "ExecStart" in text


def test_expected_unit_text_timer_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(doctor, "render_unit_timer", lambda _root, _cal: "[Timer]")
    text = doctor._expected_unit_text(cfg, TIMER_UNIT_NAME)
    assert text == "[Timer]"


def test_expected_unit_text_value_error_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)

    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise ValueError("bad render")

    monkeypatch.setattr(doctor, "render_unit_service", boom)
    assert doctor._expected_unit_text(cfg, RUN_UNIT_NAME) is None
