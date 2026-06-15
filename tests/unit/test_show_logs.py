from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import cli
from libvirt_backup_system.systemd_units import (
    MAINTENANCE_FULL_UNIT_NAME,
    MAINTENANCE_UNIT_NAME,
    RUN_UNIT_NAME,
    VERIFY_UNIT_NAME,
    show_logs,
)


def _fake_journal_host(tmp_path: Path, monkeypatch, *, units: list[str]) -> Path:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    for unit in units:
        (systemd_dir / unit).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.systemd_units.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.prefixed", lambda path, root: tmp_path / str(path).lstrip("/")
    )
    monkeypatch.setattr("libvirt_backup_system.systemd_units.journalctl_available", lambda root: True)
    return systemd_dir


def _capture_journalctl(monkeypatch, *, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, rc: int) -> None:
            self.returncode = rc

    def fake_run(args: list[str], **kwargs: Any) -> _Result:
        calls.append(args)
        return _Result(returncode)

    monkeypatch.setattr("libvirt_backup_system.systemd_units.subprocess.run", fake_run)
    return calls


def _refuse_journalctl(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.subprocess.run",
        lambda *a, **k: pytest.fail("journalctl must not be invoked"),
    )


def test_show_logs_errors_when_journalctl_unavailable(tmp_path: Path, capsys) -> None:
    # A non-root prefix means we are not on the real systemd host: journalctl
    # reads the global journal, so there is nothing to show.
    assert show_logs(str(tmp_path)) == 1
    assert "journalctl unavailable" in capsys.readouterr().err


def test_show_logs_tails_run_unit_by_default(tmp_path: Path, monkeypatch) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    calls = _capture_journalctl(monkeypatch)

    assert show_logs(None) == 0
    assert calls == [["journalctl", "--no-pager", "--output=cat", "--lines", "50", "--unit", RUN_UNIT_NAME]]


def test_show_logs_follow_appends_follow_flag(tmp_path: Path, monkeypatch) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    calls = _capture_journalctl(monkeypatch)

    assert show_logs(None, follow=True) == 0
    assert calls[0][-1] == "--follow"


def test_show_logs_all_component_tails_every_installed_unit(tmp_path: Path, monkeypatch) -> None:
    units = [RUN_UNIT_NAME, MAINTENANCE_UNIT_NAME, MAINTENANCE_FULL_UNIT_NAME, VERIFY_UNIT_NAME]
    _fake_journal_host(tmp_path, monkeypatch, units=units)
    calls = _capture_journalctl(monkeypatch)

    assert show_logs(None, component="all") == 0
    cmd = calls[0]
    assert cmd.count("--unit") == len(units)
    for unit in units:
        assert unit in cmd


def test_show_logs_lines_all_passes_through(tmp_path: Path, monkeypatch) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    calls = _capture_journalctl(monkeypatch)

    assert show_logs(None, lines="all") == 0
    cmd = calls[0]
    assert cmd[cmd.index("--lines") + 1] == "all"


def test_show_logs_rejects_invalid_lines(tmp_path: Path, monkeypatch, capsys) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    _refuse_journalctl(monkeypatch)

    assert show_logs(None, lines="-5") == 2
    assert "invalid --lines" in capsys.readouterr().err


def test_show_logs_rejects_unknown_component(tmp_path: Path, monkeypatch, capsys) -> None:
    # argparse already restricts the component, but show_logs is a public entry
    # point; guard it so a bad direct call fails cleanly instead of tailing an
    # empty unit set.
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    _refuse_journalctl(monkeypatch)

    assert show_logs(None, component="bogus") == 2
    assert "unknown log component" in capsys.readouterr().err


def test_show_logs_errors_when_units_not_installed(tmp_path: Path, monkeypatch, capsys) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[])
    _refuse_journalctl(monkeypatch)

    assert show_logs(None) == 1
    assert "backup service is not installed" in capsys.readouterr().err


def test_show_logs_propagates_journalctl_exit_code(tmp_path: Path, monkeypatch) -> None:
    _fake_journal_host(tmp_path, monkeypatch, units=[RUN_UNIT_NAME])
    _capture_journalctl(monkeypatch, returncode=4)

    assert show_logs(None) == 4


def test_cli_log_routes_to_show_logs(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_show_logs(prefix: object, *, follow: bool, lines: str, component: str) -> int:
        captured["args"] = (prefix, follow, lines, component)
        return 0

    monkeypatch.setattr(cli, "show_logs", fake_show_logs)

    assert cli.main(["log", "-f", "-n", "all", "verify"]) == 0
    assert captured["args"] == (None, True, "all", "verify")


def test_cli_logs_alias_routes_to_show_logs(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_show_logs(prefix: object, *, follow: bool, lines: str, component: str) -> int:
        captured["component"] = component
        return 0

    monkeypatch.setattr(cli, "show_logs", fake_show_logs)

    assert cli.main(["logs"]) == 0
    assert captured["component"] == "run"
