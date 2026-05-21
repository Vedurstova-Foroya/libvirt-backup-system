from __future__ import annotations

import textwrap
import time

import pytest

from libvirt_backup_system import shell
from libvirt_backup_system.shell import CommandError, configure_default_timeout, run, run_streamed


def test_configure_default_timeout_rejects_non_positive_value() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        configure_default_timeout("0")


def test_configure_default_timeout_accepts_float_string() -> None:
    # Operator-edited env values render as floats (``86400.0``) when the
    # python-side env writer round-trips through ``float``. ``int("86400.0")``
    # would error and crash the cli; the parser must accept any finite number.
    configure_default_timeout("86400.0")
    assert shell._default_timeout_seconds == 86400


def test_configure_default_timeout_rejects_non_numeric() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        configure_default_timeout("not-a-number")


def test_run_timeout_returns_command_error(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run(["python3", "-c", "import time; time.sleep(2)"], timeout=0.1)
    assert exc.value.result.returncode == shell.TIMEOUT_RETURN_CODE
    assert "command timed out" in capsys.readouterr().err


def test_run_streamed_timeout_kills_process_group(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run_streamed(["python3", "-c", "import time; print('start', flush=True); time.sleep(2)"], timeout=0.1)
    assert exc.value.result.returncode == shell.TIMEOUT_RETURN_CODE
    captured = capsys.readouterr()
    assert '"line":"start"' in captured.out
    assert "command timed out" in captured.err


class _Pipe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ZombieThread:
    """Test double: pretends to be alive until ``die_after_joins`` join calls.

    ``None`` means the thread never finishes — used to drive the drain helper
    into the close-pipes-and-abandon branch that simulates a SIGKILL-resistant
    grandchild that has detached into its own session.
    """

    def __init__(self, die_after_joins: int | None = None) -> None:
        self._joins = 0
        self._die_after = die_after_joins

    def join(self, timeout: float | None = None) -> None:
        self._joins += 1

    def is_alive(self) -> bool:
        if self._die_after is None:
            return True
        return self._joins < self._die_after


def test_tee_stream_swallows_read_errors_from_parent_close() -> None:
    # When the drain helper closes the read end of the pipe from the parent
    # process to break a wedged grandchild, readline raises ValueError on
    # the closed file. _tee_stream must treat that as a clean EOF rather
    # than dying with an unhandled exception that pollutes the logs.
    from collections import deque

    from libvirt_backup_system.shell import _tee_stream

    class _RaisingStream:
        def readline(self) -> str:
            raise ValueError("I/O operation on closed file")

        def close(self) -> None:
            return None

    buf: deque[str] = deque()
    _tee_stream(_RaisingStream(), "info", "stdout", "cmd", buf)  # must not raise
    assert list(buf) == []


def test_signal_group_returns_false_when_killpg_fails(monkeypatch) -> None:
    # An empty/missing process group is a normal terminal state for the
    # drain escalation; surface it to the caller so it stops trying to
    # signal and moves on to closing the pipes from the parent side.
    from libvirt_backup_system.shell import _signal_group

    def raise_lookup(pgid: int, sig: int) -> None:
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", raise_lookup)
    assert _signal_group(4242, 15) is False


def test_drain_stream_threads_returns_quickly_when_threads_finish() -> None:
    from libvirt_backup_system.shell import _drain_stream_threads

    threads = [_ZombieThread(die_after_joins=1), _ZombieThread(die_after_joins=1)]
    pipes = (_Pipe(), _Pipe())
    _drain_stream_threads(threads, 4242, pipes, "cmd")  # type: ignore[arg-type]
    assert all(not pipe.closed for pipe in pipes)


def test_drain_stream_threads_escalates_through_sigterm_sigkill_and_close(monkeypatch, capsys) -> None:
    # Worst case: leader gone, grandchild detached into its own session, so
    # SIGTERM/SIGKILL to the captured pgid never reach it. The drain helper
    # must close the read end of the pipes from the parent so a stuck
    # grandchild cannot pin the run lock past COMMAND_TIMEOUT_SECONDS.
    from libvirt_backup_system.shell import _drain_stream_threads

    signals: list[int] = []
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: signals.append(sig))
    threads = [_ZombieThread(die_after_joins=None), _ZombieThread(die_after_joins=None)]
    pipes = (_Pipe(), _Pipe())

    _drain_stream_threads(threads, 4242, pipes, "cmd")  # type: ignore[arg-type]

    import signal as signal_module

    assert signals == [signal_module.SIGTERM, signal_module.SIGKILL]
    assert all(pipe.closed for pipe in pipes)
    err = capsys.readouterr().err
    assert "stream drain timed out after leader exit" in err
    assert "process group kill did not release stream pipes" in err
    assert "stream reader threads still blocked after pipe close; abandoning" in err


def test_drain_stream_threads_stops_signaling_when_group_is_empty(monkeypatch) -> None:
    # If killpg reports ESRCH (group already drained) we must not keep
    # trying signals — go straight to closing the parent-side pipes so the
    # reader threads unwedge.
    from libvirt_backup_system.shell import _drain_stream_threads

    def raise_lookup(pgid: int, sig: int) -> None:
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", raise_lookup)
    threads = [_ZombieThread(die_after_joins=None)]
    pipes = (_Pipe(),)

    _drain_stream_threads(threads, 4242, pipes, "cmd")  # type: ignore[arg-type]

    assert pipes[0].closed


def test_drain_stream_threads_returns_after_sigterm_reaps_grandchild(monkeypatch, capsys) -> None:
    # Common case: SIGTERM to the process group kills the grandchild that
    # inherited the pipes, the writers close, and the reader threads exit
    # before we have to escalate to SIGKILL or parent-side close.
    from libvirt_backup_system.shell import _drain_stream_threads

    signals: list[int] = []
    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: signals.append(sig))
    # Thread alive across first join (drives into the signal path) and the
    # join right after SIGTERM (so SIGTERM is the signal that does it), then
    # finishes before the SIGKILL escalation join.
    threads = [_ZombieThread(die_after_joins=2)]
    pipes = (_Pipe(),)

    _drain_stream_threads(threads, 4242, pipes, "cmd")  # type: ignore[arg-type]

    import signal as signal_module

    assert signals == [signal_module.SIGTERM]
    assert not pipes[0].closed
    assert "stream drain timed out after leader exit" in capsys.readouterr().err


def test_drain_stream_threads_does_not_abandon_when_parent_close_unwedges(monkeypatch, capsys) -> None:
    # Closing the read end of the pipes from the parent process makes the
    # blocked readline return, the reader thread exits, and the drain
    # helper finishes cleanly. The abandon-the-thread error log only fires
    # when even that last-resort fails.
    from libvirt_backup_system.shell import _drain_stream_threads

    monkeypatch.setattr("libvirt_backup_system.shell.os.killpg", lambda pgid, sig: None)
    # 4 joins: initial, SIGTERM, SIGKILL, final-after-close. Thread becomes
    # dead on the last one (the close did its job).
    threads = [_ZombieThread(die_after_joins=4)]
    pipes = (_Pipe(),)

    _drain_stream_threads(threads, 4242, pipes, "cmd")  # type: ignore[arg-type]

    assert pipes[0].closed
    err = capsys.readouterr().err
    assert "process group kill did not release stream pipes" in err
    assert "abandoning" not in err


def test_run_streamed_does_not_block_when_grandchild_inherits_pipes(monkeypatch, capsys) -> None:
    # Regression: proc.wait() only tracks the leader. A grandchild that
    # inherits stdout/stderr keeps the parent-side pipe read open after the
    # leader exits successfully. With the previous unbounded thread.join,
    # run_streamed would block past COMMAND_TIMEOUT_SECONDS, outliving
    # systemd's TimeoutStartSec=infinity and holding the run lock until the
    # grandchild died on its own. The drain helper must escalate to killing
    # the captured process group instead of waiting forever.
    monkeypatch.setattr("libvirt_backup_system.shell.STREAM_DRAIN_GRACE_SECONDS", 0.2)
    program = textwrap.dedent("""
        import os, sys, time
        # Detach stdin so the grandchild does not block the runner.
        if os.fork() == 0:
            # Grandchild: keeps stdout/stderr open until killed via killpg.
            time.sleep(60)
            sys.exit(0)
        sys.stdout.flush()
        sys.stderr.flush()
    """)
    start = time.monotonic()
    result = run_streamed(["python3", "-c", program])
    elapsed = time.monotonic() - start
    assert result.returncode == 0
    # Without the fix this would be ~60s. With the fix: ~0.2s drain grace +
    # killpg + ~0.2s second drain grace. Allow generous margin for slow CI.
    assert elapsed < 5, f"drain took {elapsed:.2f}s; grandchild kept pipes open"
    assert "stream drain timed out after leader exit" in capsys.readouterr().err
