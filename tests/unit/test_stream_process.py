from __future__ import annotations

import os
import signal
import subprocess
from unittest.mock import MagicMock

import pytest

from libvirt_backup_system import stream_process

# ---------------------------------------------------------------------------
# popen_args
# ---------------------------------------------------------------------------


def test_popen_args_with_list() -> None:
    proc = MagicMock(spec=subprocess.Popen)
    proc.args = ["ls", "-la", "/tmp"]
    assert stream_process.popen_args(proc) == ["ls", "-la", "/tmp"]


def test_popen_args_with_tuple() -> None:
    proc = MagicMock(spec=subprocess.Popen)
    proc.args = ("echo", "hello")
    assert stream_process.popen_args(proc) == ["echo", "hello"]


def test_popen_args_with_string() -> None:
    proc = MagicMock(spec=subprocess.Popen)
    proc.args = "ls -la"
    assert stream_process.popen_args(proc) == ["ls -la"]


# ---------------------------------------------------------------------------
# timeout_message
# ---------------------------------------------------------------------------


def test_timeout_message_with_none() -> None:
    """Line 22: timeout_seconds is None."""
    assert stream_process.timeout_message("backup", None) == "backup timed out"


def test_timeout_message_with_value() -> None:
    assert stream_process.timeout_message("backup", 30.0) == "backup timed out after 30 seconds"


# ---------------------------------------------------------------------------
# command_deadline / remaining_timeout
# ---------------------------------------------------------------------------


def test_command_deadline_none() -> None:
    assert stream_process.command_deadline(None) is None


def test_command_deadline_with_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_process.time, "monotonic", lambda: 100.0)
    assert stream_process.command_deadline(10.0) == 110.0


def test_remaining_timeout_none() -> None:
    assert stream_process.remaining_timeout(None) is None


def test_remaining_timeout_with_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_process.time, "monotonic", lambda: 105.0)
    assert stream_process.remaining_timeout(110.0) == 5.0


def test_remaining_timeout_clamps_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stream_process.time, "monotonic", lambda: 120.0)
    assert stream_process.remaining_timeout(110.0) == 0.0


# ---------------------------------------------------------------------------
# _process_group_id
# ---------------------------------------------------------------------------


def test_process_group_id_returns_pgid(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 1234
    monkeypatch.setattr(os, "getpgid", lambda pid: 5678)
    assert stream_process._process_group_id(proc) == 5678


def test_process_group_id_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 66-69: os.getpgid raises OSError -> return None."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = 1234

    def _raise(pid: int) -> int:
        raise OSError("No such process")

    monkeypatch.setattr(os, "getpgid", _raise)
    assert stream_process._process_group_id(proc) is None


def test_process_group_id_returns_none_when_pid_not_int() -> None:
    """Lines 73-74 equivalent: pid is not an int -> return None."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.pid = None
    assert stream_process._process_group_id(proc) is None


def test_process_group_id_returns_none_when_no_pid_attr() -> None:
    """pid attribute missing entirely -> return None."""

    class _NoPid:
        pass

    proc = _NoPid()
    assert stream_process._process_group_id(proc) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _signal_group
# ---------------------------------------------------------------------------


def test_signal_group_calls_killpg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 73-74: normal path."""
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    stream_process._signal_group(42, signal.SIGTERM)
    assert calls == [(42, signal.SIGTERM)]


def test_signal_group_suppresses_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 73-74: OSError is suppressed."""

    def _raise(pgid: int, sig: int) -> None:
        raise OSError("No such process")

    monkeypatch.setattr(os, "killpg", _raise)
    # Should not raise
    stream_process._signal_group(42, signal.SIGTERM)


def test_signal_group_suppresses_process_lookup_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 73-74: ProcessLookupError is suppressed."""

    def _raise(pgid: int, sig: int) -> None:
        raise ProcessLookupError("No such process")

    monkeypatch.setattr(os, "killpg", _raise)
    # Should not raise
    stream_process._signal_group(42, signal.SIGKILL)


# ---------------------------------------------------------------------------
# terminate_process
# ---------------------------------------------------------------------------


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


def test_terminate_process_none() -> None:
    """proc is None -> early return."""
    stream_process.terminate_process(None)


def test_terminate_process_already_exited() -> None:
    """proc.poll() is not None -> early return."""
    proc = _FakeProc(poll_returns=0)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert not proc.terminated
    assert not proc.killed


def test_terminate_process_no_pgid_sigterm_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """pgid is None -> proc.terminate() then wait succeeds."""
    proc = _FakeProc(pgid=None)
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert proc.terminated
    assert not proc.killed


def test_terminate_process_with_pgid_sigterm_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 42: pgid is not None -> _signal_group(pgid, SIGTERM), wait succeeds."""
    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: 999)
    monkeypatch.setattr(stream_process, "_signal_group", lambda pgid, sig: signaled.append((pgid, sig)))
    proc = _FakeProc()
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert signaled == [(999, signal.SIGTERM)]
    assert not proc.terminated
    assert not proc.killed


def test_terminate_process_no_pgid_sigterm_timeout_then_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    """pgid is None, SIGTERM times out -> proc.kill() then wait."""
    wait_calls = [0]

    class _Proc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            return 0

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: None)
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert proc.terminated
    assert proc.killed


def test_terminate_process_with_pgid_sigterm_timeout_then_sigkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 52: pgid is not None, SIGTERM times out -> _signal_group(pgid, SIGKILL)."""
    signaled: list[tuple[int, int]] = []
    wait_calls = [0]

    class _Proc(_FakeProc):
        def wait(self, timeout: float | None = None) -> int:
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                raise subprocess.TimeoutExpired(["cmd"], timeout)
            return 0

    proc = _Proc()
    monkeypatch.setattr(stream_process, "_process_group_id", lambda p: 999)
    monkeypatch.setattr(stream_process, "_signal_group", lambda pgid, sig: signaled.append((pgid, sig)))
    stream_process.terminate_process(proc)  # type: ignore[arg-type]
    assert signaled == [(999, signal.SIGTERM), (999, signal.SIGKILL)]
    assert not proc.killed
