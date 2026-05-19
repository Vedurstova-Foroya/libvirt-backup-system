from __future__ import annotations

import os
import signal
import subprocess
import textwrap
import time

import pytest

from libvirt_backup_system import shell
from libvirt_backup_system.shell import CommandError, run


class _StubPopen:
    """Popen double whose communicate() always raises TimeoutExpired.

    ``shell.run`` calls communicate once for the primary timeout escalation,
    then again after _kill_process_group to drain leftover bytes. The double
    surfaces stdout/stderr from the first timeout exception so the test can
    assert that bytes/None inputs decode correctly.
    """

    def __init__(self, output, stderr) -> None:
        self.pid = 1234
        self.returncode = None
        self._timeout = subprocess.TimeoutExpired(cmd=["cmd"], timeout=1, output=output, stderr=stderr)

    def communicate(self, timeout=None):
        del timeout
        raise self._timeout

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def test_run_timeout_check_false_returns_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.shell.subprocess.Popen",
        lambda *a, **k: _StubPopen(output=b"out", stderr=b"err"),
    )
    monkeypatch.setattr("libvirt_backup_system.shell._kill_process_group", lambda proc, pgid: None)
    result = run(["cmd"], check=False)
    assert result.returncode == shell.TIMEOUT_RETURN_CODE
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_run_timeout_preserves_string_output(monkeypatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.shell.subprocess.Popen",
        lambda *a, **k: _StubPopen(output="out", stderr=None),
    )
    monkeypatch.setattr("libvirt_backup_system.shell._kill_process_group", lambda proc, pgid: None)
    result = run(["cmd"], check=False)
    assert result.stdout == "out"
    assert result.stderr == ""


def test_run_kills_process_group_on_keyboard_interrupt(monkeypatch) -> None:
    # KeyboardInterrupt mid-communicate must trigger process-group kill so a
    # Ctrl-C in a long preflight cannot leave virsh running.
    killed: list[object] = []

    class _InterruptedPopen:
        pid = 4242
        returncode = None

        def communicate(self, timeout=None):
            del timeout
            raise KeyboardInterrupt

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *a, **k: _InterruptedPopen())
    monkeypatch.setattr("libvirt_backup_system.shell._kill_process_group", lambda proc, pgid: killed.append(proc))
    with pytest.raises(KeyboardInterrupt):
        run(["cmd"], check=False)
    assert killed


def test_run_timeout_falls_back_to_exception_output_when_drain_hangs(monkeypatch) -> None:
    # Post-kill drain communicate() can itself time out (the grandchild ignored
    # SIGTERM/SIGKILL and still holds the pipe). Fall back to the output
    # captured by the original TimeoutExpired instead of hanging.
    timeout_exc = subprocess.TimeoutExpired(cmd=["cmd"], timeout=1, output="partial", stderr="oops")

    class _DoubleTimeoutPopen:
        pid = 4242
        returncode = None

        def communicate(self, timeout=None):
            del timeout
            raise timeout_exc

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *a, **k: _DoubleTimeoutPopen())
    monkeypatch.setattr("libvirt_backup_system.shell._kill_process_group", lambda proc, pgid: None)
    result = run(["cmd"], check=False)
    assert result.returncode == shell.TIMEOUT_RETURN_CODE
    assert result.stdout == "partial"
    assert result.stderr == "oops"


def test_run_timeout_signals_group_after_leader_exited_with_open_pipe(monkeypatch) -> None:
    # communicate(timeout=...) can expire after the leader has already exited
    # if a child inherited stdout/stderr and keeps the pipe open. The captured
    # process group still needs a signal in that state.
    killed: list[int] = []

    class _ExitedLeaderOpenPipePopen:
        pid = 4242
        returncode = 0

        def __init__(self) -> None:
            self._communicate_calls = 0

        def communicate(self, timeout=None):
            self._communicate_calls += 1
            if self._communicate_calls == 1:
                raise subprocess.TimeoutExpired(cmd=["cmd"], timeout=timeout)
            return "", ""

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.Popen", lambda *a, **k: _ExitedLeaderOpenPipePopen())
    monkeypatch.setattr("libvirt_backup_system.shell.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: killed.append(sig))

    result = run(["cmd"], check=False, timeout=1)

    assert result.returncode == shell.TIMEOUT_RETURN_CODE
    assert killed == [signal.SIGTERM, signal.SIGKILL]


def test_run_timeout_kills_grandchildren(tmp_path) -> None:
    # subprocess.run(timeout=...) only signals the leader; a double-forking
    # grandchild survives. shell.run must kill the whole process group.
    sentinel = tmp_path / "alive"
    program = textwrap.dedent(f"""
        import os, sys, time
        if os.fork() == 0:
            with open({str(sentinel)!r}, "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(60)
            sys.exit(0)
        time.sleep(5)
    """)
    with pytest.raises(CommandError) as exc:
        run(["python3", "-c", program], timeout=1.0)
    assert exc.value.result.returncode == shell.TIMEOUT_RETURN_CODE
    grandchild_pid = int(sentinel.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"grandchild {grandchild_pid} survived shell.run timeout")
