from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from libvirt_backup_system.systemd_units import (
    DISPATCH_OPT_OUT_ENV,
    RUN_UNIT_NAME,
    TIMER_UNIT_NAME,
    dispatch_via_systemd,
)


def _fake_systemd_host(tmp_path: Path, monkeypatch) -> Path:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    fake_prefixed = lambda path, root: tmp_path / str(path).lstrip("/")  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.systemd_units.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr("libvirt_backup_system.systemd_units.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.systemd_run_gate.prefixed", fake_prefixed)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")
    original_exists = Path.exists
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.Path.exists",
        lambda self: True if str(self) == "/run/systemd/system" else original_exists(self),
    )
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv(DISPATCH_OPT_OUT_ENV, raising=False)
    return systemd_dir


def _record_subprocess(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args: list[str], **kwargs: Any) -> _Result:
        calls.append(args)
        if args == [
            "systemctl",
            "show",
            TIMER_UNIT_NAME,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=UnitFileState",
            "--value",
        ]:
            return _Result(0, "loaded\nactive\nenabled\n")
        # A manual ``run`` enqueues the oneshot service detached and returns
        # immediately; it no longer blocks on ``start --wait`` or tails the
        # journal inline (the operator follows it with ``log -f``).
        if args[:3] == ["systemctl", "start", "--no-block"]:
            return _Result(0)
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_dispatch_opt_out_env_zero_does_not_skip_run_gate(tmp_path: Path, monkeypatch) -> None:
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text("[Timer]\n", encoding="utf-8")
    monkeypatch.setenv(DISPATCH_OPT_OUT_ENV, "0")
    calls = _record_subprocess(monkeypatch)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 0
    assert calls[0][:3] == ["systemctl", "show", TIMER_UNIT_NAME]
    assert calls[1] == ["systemctl", "start", "--no-block", RUN_UNIT_NAME]


def test_dispatch_run_starts_detached_and_returns_immediately(tmp_path: Path, monkeypatch, capsys) -> None:
    # The gate passes (timer active+enabled), so dispatch enqueues the unit
    # with ``--no-block`` and returns 0 without waiting for the backup to
    # finish. The operator is told how to follow it.
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text("[Timer]\n", encoding="utf-8")
    calls = _record_subprocess(monkeypatch)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 0
    assert ["systemctl", "start", "--no-block", RUN_UNIT_NAME] in calls
    # No blocking ``start --wait`` and no inline journal tail.
    assert not any(call[:3] == ["systemctl", "start", "--wait"] for call in calls)
    assert not any(call[:1] == ["journalctl"] for call in calls)
    out = capsys.readouterr().out
    assert "backup started in background" in out
    assert "libvirt-backup-system log -f" in out


def test_dispatch_run_reports_start_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    # If ``systemctl start --no-block`` itself fails, surface its exit code and
    # stderr rather than pretending the backup launched.
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text("[Timer]\n", encoding="utf-8")

    class _Result:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args: list[str], **kwargs: Any) -> _Result:
        if args[:2] == ["systemctl", "show"]:
            return _Result(0, "loaded\nactive\nenabled\n")
        if args[:3] == ["systemctl", "start", "--no-block"]:
            return _Result(5, stderr="Failed to start unit: boom")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 5
    err = capsys.readouterr().err
    assert "failed to start backup service" in err
    assert "boom" in err


def test_dispatch_run_errors_when_service_not_started(tmp_path: Path, monkeypatch, capsys) -> None:
    _fake_systemd_host(tmp_path, monkeypatch)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 1

    err = capsys.readouterr().err
    assert "backup service is not running" in err
    assert "run start before run" in err


def test_dispatch_run_errors_when_timer_unit_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 1

    err = capsys.readouterr().err
    assert "backup service is not running" in err
    assert TIMER_UNIT_NAME in err


def test_dispatch_run_errors_when_timer_inactive(tmp_path: Path, monkeypatch, capsys) -> None:
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    (systemd_dir / TIMER_UNIT_NAME).write_text("[Timer]\n", encoding="utf-8")

    class _Result:
        returncode = 0
        stdout = "loaded\ninactive\ndisabled\n"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _Result())

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 1
    err = capsys.readouterr().err
    assert "backup service is not running" in err
    assert "inactive" in err
