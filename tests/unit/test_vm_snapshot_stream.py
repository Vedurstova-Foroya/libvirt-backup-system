"""Process-pipeline tests for ``LibvirtSnapshotter.stream_disk``.

Split out of ``test_vm_snapshot.py`` so each file stays under the
project's 300-LOC ceiling. Shares the ``_FakePopen`` / ``_FakeStream``
pattern; uses a fake clock to keep the deadline loop fast.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import vm_snapshot
from libvirt_backup_system.shell import CommandError


class _FakeStream:
    def __init__(self, payload: bytes = b"") -> None:
        self._payload = payload
        self.closed = False

    def read(self) -> bytes:
        if self.closed:
            return b""
        self.closed = True
        return self._payload

    def close(self) -> None:
        self.closed = True


class _FakePopen:
    instances: list[_FakePopen] = []
    create_socket: bool = True

    def __init__(self, args: list[str], **kwargs: Any) -> None:
        type(self).instances.append(self)
        self.args = args
        self.kwargs = kwargs
        self.returncode: int | None = 0
        self.stdout: object | None = None
        self.stderr = _FakeStream(b"")
        if "qemu-nbd" in args[0] and type(self).create_socket:
            sock = next((Path(arg.split("=", 1)[1]) for arg in args if arg.startswith("--socket=")), None)
            if sock is not None:
                sock.touch()

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


class _MonotonicClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 10.0
        return self.t


def test_stream_disk_starts_qemu_nbd_and_nbdcopy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []
    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(vm_snapshot.secrets, "token_hex", lambda _n: "socktoken")
    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    socket = tmp_path / "vnbd-socktoken.sock"
    with snap.stream_disk(tmp_path / "base.qcow2") as proc:
        assert proc is _FakePopen.instances[-1]
    assert _FakePopen.instances[0].args == [
        "qemu-nbd",
        "-r",
        "--persistent",
        "--shared=4",
        f"--socket={socket}",
        str(tmp_path / "base.qcow2"),
    ]
    assert _FakePopen.instances[1].args == ["nbdcopy", f"nbd+unix:///?socket={socket}", "-"]


def test_stream_disk_raises_when_qemu_nbd_dies_before_socket(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class DeadOnArrival(_FakePopen):
        create_socket = False

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            super().__init__(args, **kwargs)
            self.returncode = 9

    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", DeadOnArrival)
    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    with pytest.raises(CommandError), snap.stream_disk(tmp_path / "base.qcow2"):
        pass


def test_stream_disk_terminates_processes_on_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class Hung(_FakePopen):
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            super().__init__(args, **kwargs)
            self.returncode = None

        def wait(self, timeout: float | None = None) -> int:
            if timeout is not None and self.returncode is None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return self.returncode or -9

    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", Hung)
    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    with snap.stream_disk(tmp_path / "base.qcow2"):
        pass
    assert all(inst.returncode in {-9, -15} for inst in _FakePopen.instances)


def test_stream_disk_socket_never_appears(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class NoSocket(_FakePopen):
        create_socket = False

    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", NoSocket)
    monkeypatch.setattr(vm_snapshot.time, "monotonic", _MonotonicClock())
    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    with pytest.raises(CommandError), snap.stream_disk(tmp_path / "base.qcow2"):
        pass


def test_stream_disk_socket_wait_is_capped_by_command_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class RunningNoSocket(_FakePopen):
        create_socket = False

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            super().__init__(args, **kwargs)
            self.returncode = None

    class SmallStepClock:
        def __init__(self) -> None:
            self.t = 0.0

        def __call__(self) -> float:
            self.t += 0.04
            return self.t

    clock = SmallStepClock()
    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", RunningNoSocket)
    monkeypatch.setattr(vm_snapshot.time, "monotonic", clock)
    monkeypatch.setattr(vm_snapshot.time, "sleep", lambda _seconds: None)
    snap = vm_snapshot.LibvirtSnapshotter(
        libvirt_uri="qemu:///system", socket_root=tmp_path, command_timeout_seconds=0.1
    )
    with pytest.raises(CommandError), snap.stream_disk(tmp_path / "base.qcow2"):
        pass
    assert clock.t < 1.0


def test_stream_disk_kills_processes_that_ignore_terminate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _FakePopen.instances = []

    class Stubborn(_FakePopen):
        def terminate(self) -> None:
            return

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                if timeout is not None:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(vm_snapshot.subprocess, "Popen", Stubborn)
    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    with snap.stream_disk(tmp_path / "base.qcow2"):
        for inst in _FakePopen.instances:
            inst.returncode = None


def test_command_result_handles_missing_stderr(tmp_path: Path) -> None:
    class NoStderr:
        args = ["qemu-nbd"]
        returncode = 3
        stderr = None

    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    result = snap._command_result(NoStderr(), ["qemu-nbd"], "boom")  # type: ignore[arg-type]
    assert result.returncode == 3
    assert "boom" in result.stderr


def test_command_result_falls_back_when_returncode_is_zero(tmp_path: Path) -> None:
    class HappyExit:
        args = ["qemu-nbd"]
        returncode = 0
        stderr = None

    snap = vm_snapshot.LibvirtSnapshotter(libvirt_uri="qemu:///system", socket_root=tmp_path)
    result = snap._command_result(HappyExit(), ["qemu-nbd"], "boom")  # type: ignore[arg-type]
    assert result.returncode == 1
