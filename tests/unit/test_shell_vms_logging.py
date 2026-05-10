from __future__ import annotations

import json
import signal
import subprocess

import pytest

from libvirt_backup_system import shell
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
    assert '"level":"error"' not in out.out
    assert '"line":"err1"' in out.out
    assert '"stream":"stderr"' in out.out
    assert out.err == ""


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
    assert '"level":"error"' in captured.err
    assert '"message":"command failed"' in captured.err


def test_run_streamed_check_failure_logs_stderr_tail_as_error(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run_streamed(
            [
                "python3",
                "-c",
                "import sys\nsys.stderr.write('warn\\nboom\\n')\nsys.exit(9)",
            ],
            tail_lines=1,
        )
    captured = capsys.readouterr()

    assert exc.value.result.stderr.splitlines() == ["boom"]
    assert '"line":"warn"' in captured.out
    assert '"line":"boom"' in captured.out
    assert '"level":"error"' not in captured.out
    assert '"level":"error"' in captured.err
    assert '"message":"command failed"' in captured.err
    assert '"stderr":"boom"' in captured.err


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


def test_vm_inactive_only_for_shut_off() -> None:
    assert VM("beta", " shut off ").inactive
    assert not VM("alpha", "running").inactive
    for transitional in ("paused", "in shutdown", "crashed", "pmsuspended", "blocked"):
        assert not VM("gamma", transitional).inactive, transitional


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


def test_list_vms_raises_when_state_lookup_fails(monkeypatch, capsys) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(
        args: list[str], *, check: bool = True, env: object = None, timeout: float | None = None
    ) -> CommandResult:
        if "list" in args:
            return CommandResult(args, 0, "alpha\nbeta\n", "")
        if args[-1] == "beta":
            raise CommandError(CommandResult(args, 1, "", "gone"))
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    with pytest.raises(CommandError):
        list_vms(cfg)
    err = capsys.readouterr().err
    assert "VM state discovery failed" in err
    assert "beta" in err


def test_command_error_accepts_result() -> None:
    result = CommandResult(["cmd"], 1, "", "err")
    assert CommandError(result).result is result


class _FakeStream:
    def readline(self) -> str:
        return ""

    def close(self) -> None:
        return None


class _FakeProc:
    def __init__(self, *, wait_raises: BaseException | None = None, poll_alive: bool = True) -> None:
        self.pid = 12345
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self._wait_raises = wait_raises
        self._wait_called = False
        self._terminated = False
        self._poll_alive = poll_alive

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
        if self._wait_raises is not None and not self._wait_called:
            self._wait_called = True
            raise self._wait_raises
        return 0

    def poll(self) -> int | None:
        if self._terminated or not self._poll_alive:
            return 0
        return None


def test_run_streamed_kills_process_group_on_exception(monkeypatch) -> None:
    killed: list[tuple[int, int]] = []
    proc = _FakeProc(wait_raises=RuntimeError("simulated"))

    def fake_killpg(pgid: int, sig: int) -> None:
        killed.append((pgid, sig))
        proc._terminated = True

    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("libvirt_backup_system.shell.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", fake_killpg)

    with pytest.raises(RuntimeError, match="simulated"):
        run_streamed(["dummy"])

    assert killed == [(proc.pid, signal.SIGTERM)]


def test_run_streamed_escalates_to_sigkill_after_timeout(monkeypatch) -> None:
    killed: list[int] = []

    class StubbornProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(wait_raises=KeyboardInterrupt())

        def wait(self, timeout: float | None = None) -> int:
            if not self._wait_called:
                self._wait_called = True
                raise KeyboardInterrupt
            if signal.SIGKILL not in killed and timeout is not None:
                raise subprocess.TimeoutExpired(cmd="dummy", timeout=timeout)
            return 0

    proc = StubbornProc()
    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("libvirt_backup_system.shell.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: killed.append(sig))

    with pytest.raises(KeyboardInterrupt):
        run_streamed(["dummy"])

    assert killed == [signal.SIGTERM, signal.SIGKILL]


def test_run_streamed_skips_kill_when_process_already_exited(monkeypatch) -> None:
    proc = _FakeProc(wait_raises=RuntimeError("after-exit"), poll_alive=False)
    killed: list[int] = []
    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: killed.append(sig))

    with pytest.raises(RuntimeError, match="after-exit"):
        run_streamed(["dummy"])

    assert killed == []


def test_run_streamed_falls_through_when_sigkill_also_times_out(monkeypatch) -> None:
    killed: list[int] = []

    class ZombieProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(wait_raises=RuntimeError("ignored"))

        def wait(self, timeout: float | None = None) -> int:
            if not self._wait_called:
                self._wait_called = True
                raise RuntimeError("trigger")
            if timeout is not None:
                raise subprocess.TimeoutExpired(cmd="dummy", timeout=timeout)
            return 0

    proc = ZombieProc()
    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("libvirt_backup_system.shell.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: killed.append(sig))

    with pytest.raises(RuntimeError, match="trigger"):
        run_streamed(["dummy"])

    assert killed == [signal.SIGTERM, signal.SIGKILL]


def test_run_streamed_swallows_killpg_oserror(monkeypatch) -> None:
    proc = _FakeProc(wait_raises=RuntimeError("escape"))

    def raise_oserror(pgid: int, sig: int) -> None:
        raise OSError("no such process group")

    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr("libvirt_backup_system.shell.os.getpgid", lambda pid: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", raise_oserror)

    with pytest.raises(RuntimeError, match="escape"):
        run_streamed(["dummy"])


def test_kill_process_group_module_constants() -> None:
    assert shell.TERMINATE_GRACE_SECONDS > 0


def test_list_vms_rejects_unsafe_vm_name(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    for unsafe in ("-evil", "..", "a/b"):

        def fake_run(args: list[str], *, check: bool = True, env: object = None, _value: str = unsafe) -> CommandResult:
            return CommandResult(args, 0, f"{_value}\n", "")

        monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
        with pytest.raises(ValueError, match="unsafe VM name"):
            list_vms(cfg)
