"""Tests for ``stream_process.terminate_process`` edge cases:
SIGKILL fallback paths, OSError suppression, and terminate_processes.

Split from ``test_stream_process.py`` to stay within the 300-line limit.
"""

from __future__ import annotations

import subprocess

import pytest

from libvirt_backup_system import stream_process


class _FakeProc:
    """Minimal Popen double for terminate_process tests."""

    def __init__(
        self,
        *,
        pid: int | None = 1234,
        poll_returns: int | None = None,
        wait_side_effect: Exception | None = None,
        pgid: int | None = None,
    ) -> None:
        self.pid = pid
        self._poll_returns = poll_returns
        self._wait_side_effect = wait_side_effect
        self._pgid = pgid
        self.terminated = False
        self.killed = False
        self.returncode = poll_returns

    def poll(self) -> int | None:
        return self._poll_returns

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_side_effect is not None:
            raise self._wait_side_effect
        return 0


def test_terminate_process_sigkill_wait_timeout_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 66-69: wait after SIGKILL also times out -> TimeoutExpired suppressed."""

    class _Proc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(["cmd"], timeout)

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    # Should not raise despite both waits timing out
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert proc.terminated
    assert proc.killed


def test_terminate_process_sigkill_wait_oserror_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 66-69: wait after SIGKILL raises OSError -> suppressed."""
    wait_calls = [0]

    class _Proc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            raise OSError("No such process")

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert proc.killed


def test_terminate_process_terminate_oserror_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """proc.terminate() raises OSError -> suppressed."""

    class _Proc(_FakeProc):
        def terminate(self) -> None:
            raise OSError("No such process")

        def wait(self, timeout: float | None = None) -> int:
            return 0

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]


def test_terminate_process_kill_oserror_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    """proc.kill() raises OSError -> suppressed via the with suppress block."""
    wait_calls = [0]

    class _Proc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            return 0

        def kill(self) -> None:
            raise OSError("No such process")

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# terminate_processes
# ---------------------------------------------------------------------------


def test_terminate_processes_multiple(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated: list[object] = []
    monkeypatch.setattr(stream_process, "terminate_process", lambda proc: terminated.append(proc))
    a, b, c = object(), None, object()
    stream_process.terminate_processes(a, b, c)  # type: ignore[arg-type]
    assert terminated == [a, b, c]
