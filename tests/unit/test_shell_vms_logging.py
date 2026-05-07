from __future__ import annotations

import json
import subprocess

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.logging_json import event
from libvirt_backup_system.shell import CommandError, CommandResult, run
from libvirt_backup_system.vms import VM, list_vms


def test_run_success_and_non_check_failure() -> None:
    ok = run(["python3", "-c", "print('ok')"])
    assert ok.returncode == 0
    assert ok.stdout.strip() == "ok"

    failed = run(["python3", "-c", "import sys; sys.exit(4)"], check=False)
    assert failed.returncode == 4


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


def test_command_error_accepts_result() -> None:
    result = CommandResult(["cmd"], 1, "", "err")
    assert CommandError(result).result is result
    assert subprocess.CompletedProcess(["x"], 0).returncode == 0
