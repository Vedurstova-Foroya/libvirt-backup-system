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
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    MAINTENANCE_TIMER_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    VERIFY_TIMER_NAME,
    VERIFY_UNIT_NAME,
)
from tests.unit._doctor_helpers import (
    make_config,
    make_install_files,
    stub_preflight,
    stub_systemctl_off,
)


def _stub_repo_local_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    from libvirt_backup_system import kopia_snapshots

    monkeypatch.setattr(kopia_repo, "local_repo_exists", lambda _cfg: True)
    monkeypatch.setattr(kopia_repo, "local_config_file", lambda cfg: cfg.prefix / "kopia.config")
    monkeypatch.setattr(kopia_repo, "password_file_path", lambda cfg: cfg.prefix / "pw")
    monkeypatch.setattr(kopia_repo, "cache_dir", lambda cfg: cfg.prefix / "cache")
    monkeypatch.setattr(kopia_client, "repository_status", lambda **_: {"ok": True})
    monkeypatch.setattr(kopia_client, "maintenance_info", lambda **_: None)
    monkeypatch.setattr(kopia_snapshots, "snapshot_verify", lambda **_: None)
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


def test_doctor_passes_but_emits_quiesce_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Past QGA quiesce fallbacks must surface as advisories, NOT failures.

    The plan calls the fallback "must be visible in doctor"; the check
    landing as an info/warning preserves the rc=0 of a healthy host while
    still flagging the crash-consistent backup so operators can install
    qemu-guest-agent.
    """
    import json as _json

    cfg = make_config(tmp_path, with_backup_path=False)
    make_install_files(cfg)
    stub_preflight(monkeypatch)
    _stub_repo_local_ok(monkeypatch)
    # systemctl is on only for journalctl path; runtime-state check needs
    # to stay quiet, so the helper below masks the timer values to a clean
    # state. With BACKUP_PATH unset the runtime-state check still runs but
    # the systemctl shows return inert values that bypass each failure
    # branch.
    monkeypatch.setattr(doctor, "systemctl_available", lambda _root: True)

    advisory_line = _json.dumps(
        {
            "ts": "2026-05-22T01:00:00.000000Z",
            "level": "warning",
            "message": doctor.QUIESCE_FALLBACK_MESSAGE,
            "vm": "vm-orange",
        }
    )

    def fake_run(args: list[str], **_kwargs: object) -> object:
        from libvirt_backup_system.shell import CommandResult

        if args[:1] == ["journalctl"]:
            return CommandResult(args, 0, advisory_line, "")
        if args[:2] == ["systemctl", "show"]:
            prop = args[3].split("=", 1)[1]
            healthy = {
                "UnitFileState": "enabled",
                "ActiveState": "active",
                "NeedDaemonReload": "no",
                "Result": "success",
                "LastTriggerUSec": "1",
                "NextElapseUSecRealtime": "2",
            }
            return CommandResult(args, 0, healthy.get(prop, ""), "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(doctor, "run", fake_run)

    assert doctor.doctor(cfg) == 0

    err = capsys.readouterr().err
    assert "doctor advisory" in err
    assert "vm-orange" in err


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


def test_expected_unit_text_maintenance_service_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, MAINTENANCE_UNIT_NAME)
    assert text is not None
    assert "kopia-passthrough -- maintenance run --safety=full" in text


def test_expected_unit_text_verify_service_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, VERIFY_UNIT_NAME)
    assert text is not None
    assert " --config " in text
    assert " verify" in text
    assert "kopia-passthrough" not in text


def test_expected_unit_text_maintenance_timer_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, MAINTENANCE_TIMER_NAME)
    assert text is not None
    assert "OnActiveSec=15min" in text
    assert "OnBootSec" not in text
    assert "OnUnitActiveSec=24h" in text


def test_expected_unit_text_verify_timer_branch(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    text = doctor._expected_unit_text(cfg, VERIFY_TIMER_NAME)
    assert text is not None
    assert "OnActiveSec=75min" in text
    assert "OnBootSec" not in text
    assert "OnUnitActiveSec=7d" in text
