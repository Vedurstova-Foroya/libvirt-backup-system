from __future__ import annotations

import json

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.logging_json import event
from libvirt_backup_system.shell import CommandError, CommandResult, run, run_streamed
from libvirt_backup_system.vms import VM, list_vms


def test_run_success_and_non_check_failure() -> None:
    ok = run(["python3", "-c", "print('ok')"])
    assert ok.returncode == 0
    assert ok.stdout.strip() == "ok"

    failed = run(["python3", "-c", "import sys; sys.exit(4)"], check=False)
    assert failed.returncode == 4


def test_run_streamed_success_tees_lines_and_returns_tail(capsys) -> None:
    result = run_streamed(["python3", "-c", "import sys\nprint('a')\nprint('b')\nsys.stderr.write('err1\\n')"])
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["a", "b"]
    assert result.stderr.splitlines() == ["err1"]
    out = capsys.readouterr()
    assert '"line":"a"' in out.out
    assert '"line":"b"' in out.out
    assert '"line":"err1"' in out.err


def test_run_streamed_check_failure_includes_tail(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run_streamed(
            [
                "python3",
                "-c",
                "import sys\nfor i in range(5): print(i)\nsys.exit(7)",
            ],
            tail_lines=3,
        )
    captured = capsys.readouterr()
    assert exc.value.result.returncode == 7
    assert exc.value.result.stdout.splitlines() == ["2", "3", "4"]
    assert '"line":"0"' in captured.out


def test_run_streamed_non_check_returns_result() -> None:
    result = run_streamed(["python3", "-c", "import sys; sys.exit(3)"], check=False)
    assert result.returncode == 3


def test_run_check_failure() -> None:
    with pytest.raises(CommandError) as exc:
        run(["python3", "-c", "import sys; print('bad'); sys.exit(5)"])
    assert exc.value.result.returncode == 5
    assert "command failed (5)" in str(exc.value)


def test_event_streams(capsys) -> None:
    event("info", "hello", count=1)
    event("error", "bad")
    captured = capsys.readouterr()
    assert json.loads(captured.out)["message"] == "hello"
    assert json.loads(captured.err)["level"] == "error"


def test_vm_running_property() -> None:
    assert VM("alpha", " running ").running
    assert not VM("beta", "shut off").running


def test_list_vms_filters_blacklist(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    cfg.values["VM_BLACKLIST"] = "beta"
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        if "list" in args:
            return CommandResult(args, 0, "alpha\nbeta\n\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert list_vms(cfg) == [VM("alpha", "running")]
    assert list_vms(cfg, include_blacklisted=True) == [VM("alpha", "running"), VM("beta", "running")]
    assert calls[0][:3] == ["virsh", "-c", "qemu:///system"]
    domstate_calls = [call for call in calls if "domstate" in call]
    assert domstate_calls, "expected at least one domstate call"
    for call in domstate_calls:
        assert call[-2:-1] == ["--"]


def test_command_error_accepts_result() -> None:
    result = CommandResult(["cmd"], 1, "", "err")
    assert CommandError(result).result is result


def test_list_vms_rejects_vm_name_starting_with_dash(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "-evil\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    with pytest.raises(ValueError, match="begins with a dash"):
        list_vms(cfg)
