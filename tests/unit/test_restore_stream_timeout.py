from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import restore, restore_io, stream_process

from .restore_helpers import ConvertOk, KopiaProc, run_stream


class _ConvertTimeout:
    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.args = ["qemu-img", "convert"]
        self.returncode: int | None = None
        self.terminated = False

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        raise subprocess.TimeoutExpired(self.args, timeout)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode or -15

    def kill(self) -> None:
        self.returncode = -9


class _KopiaWaitTimeout(KopiaProc):
    def __init__(self) -> None:
        super().__init__(returncode=None)

    def wait(self, timeout: float | None = None) -> None:
        if self.returncode is not None:
            return
        raise subprocess.TimeoutExpired(["kopia", "snapshot", "restore"], timeout)


def test_stream_disk_to_qcow2_times_out_convert_and_kills_kopia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kopia_proc = KopiaProc(returncode=None)
    assert run_stream(restore, tmp_path, monkeypatch, kopia=kopia_proc, popen=_ConvertTimeout) is False
    assert "restore stream timed out" in capsys.readouterr().err
    assert kopia_proc.returncode == -15


def test_stream_disk_to_qcow2_times_out_kopia_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kopia_proc = _KopiaWaitTimeout()
    assert run_stream(restore, tmp_path, monkeypatch, kopia=kopia_proc, popen=ConvertOk) is False
    assert "restore stream timed out" in capsys.readouterr().err
    assert kopia_proc.returncode == -15


def test_stream_disk_to_qcow2_reuses_deadline_for_kopia_wait(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter([100.0, 104.0, 109.5])
    convert_timeouts: list[float | None] = []

    class RecordingConvert(ConvertOk):
        def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
            convert_timeouts.append(timeout)
            return super().communicate(timeout)

    class RecordingKopia(KopiaProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self.wait_timeouts: list[float | None] = []

        def wait(self, timeout: float | None = None) -> None:
            self.wait_timeouts.append(timeout)
            super().wait(timeout)

    kopia_proc = RecordingKopia()
    monkeypatch.setattr(restore_io, "_command_timeout", lambda _cfg: 10)
    monkeypatch.setattr(stream_process.time, "monotonic", lambda: next(times))
    assert run_stream(restore, tmp_path, monkeypatch, kopia=kopia_proc, popen=RecordingConvert) is True
    assert convert_timeouts == [6.0]
    assert kopia_proc.wait_timeouts == [0.5]
