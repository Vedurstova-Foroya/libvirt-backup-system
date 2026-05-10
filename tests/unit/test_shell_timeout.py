from __future__ import annotations

import subprocess

import pytest

from libvirt_backup_system import shell
from libvirt_backup_system.shell import CommandError, configure_default_timeout, run, run_streamed


def test_configure_default_timeout_rejects_non_positive_value() -> None:
    with pytest.raises(ValueError, match="greater than 0"):
        configure_default_timeout("0")


def test_run_timeout_returns_command_error(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run(["python3", "-c", "import time; time.sleep(2)"], timeout=0.1)
    assert exc.value.result.returncode == shell.TIMEOUT_RETURN_CODE
    assert "command timed out" in capsys.readouterr().err


def test_run_timeout_check_false_returns_result(monkeypatch) -> None:
    timeout = subprocess.TimeoutExpired(cmd=["cmd"], timeout=1, output=b"out", stderr=b"err")
    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(timeout))
    result = run(["cmd"], check=False)
    assert result.returncode == shell.TIMEOUT_RETURN_CODE
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_run_timeout_preserves_string_output(monkeypatch) -> None:
    timeout = subprocess.TimeoutExpired(cmd=["cmd"], timeout=1, output="out", stderr=None)
    monkeypatch.setattr("libvirt_backup_system.shell.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(timeout))
    result = run(["cmd"], check=False)
    assert result.stdout == "out"
    assert result.stderr == ""


def test_run_streamed_timeout_kills_process_group(capsys) -> None:
    with pytest.raises(CommandError) as exc:
        run_streamed(["python3", "-c", "import time; print('start', flush=True); time.sleep(2)"], timeout=0.1)
    assert exc.value.result.returncode == shell.TIMEOUT_RETURN_CODE
    captured = capsys.readouterr()
    assert '"line":"start"' in captured.out
    assert "command timed out" in captured.err
