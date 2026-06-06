from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import restore_state
from libvirt_backup_system.shell import CommandError, CommandResult

from .restore_helpers import make_config


def test_restore_vm_power_starts_running_vm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore_state, "run", fake_run)
    assert restore_state.restore_vm_power(make_config(tmp_path), "myvm", " running ") is True
    assert calls == [["virsh", "-c", "qemu:///system", "start", "--", "myvm"]]


def test_restore_vm_power_leaves_off_vm_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore_state, "run", lambda *_a, **_kw: pytest.fail("must not start VM"))
    assert restore_state.restore_vm_power(make_config(tmp_path), "myvm", "shut off") is True


@pytest.mark.parametrize(
    "raises,message",
    [
        (CommandError(CommandResult(["virsh"], 1, "", "start failed")), "restored VM start failed"),
        (OSError("no virsh"), "virsh start unavailable"),
    ],
    ids=["command-error", "os-error"],
)
def test_restore_vm_power_start_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raises: BaseException,
    message: str,
) -> None:
    def boom(*_a: Any, **_kw: Any) -> CommandResult:
        raise raises

    monkeypatch.setattr(restore_state, "run", boom)
    assert restore_state.restore_vm_power(make_config(tmp_path), "myvm", "running") is False
    assert message in capsys.readouterr().err
