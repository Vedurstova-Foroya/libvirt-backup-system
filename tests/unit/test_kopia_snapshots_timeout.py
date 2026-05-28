from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots
from libvirt_backup_system.shell import TIMEOUT_RETURN_CODE


def _write_password(path: Path) -> Path:
    path.write_text("swordfish", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def _fake_kopia_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_snapshots, "build_kopia_env", lambda *_a, **_kw: {})


class _KopiaSuccess:
    def __init__(self, args: list[str], **_: Any) -> None:
        self.args = args
        self.returncode = 0

    def communicate(self, _input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        _ = timeout
        return ('{"id":"snap-stdin"}', "")


class _Terminable:
    args = ["nbdcopy"]

    def __init__(self) -> None:
        self.stdout: object = None
        self.returncode: int | None = None
        self.terminated = False

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


def test_snapshot_create_stdin_times_out_kopia_and_terminates_upstream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    password = _write_password(tmp_path / "pw")

    class TimeoutKopia(_Terminable):
        def __init__(self, args: list[str], **_: Any) -> None:
            super().__init__()
            self.args = args

        def communicate(self, _input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
            raise subprocess.TimeoutExpired(self.args, timeout)

    upstream = _Terminable()
    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", TimeoutKopia)
    with pytest.raises(kopia_snapshots.SnapshotCreateError) as info:
        kopia_snapshots.snapshot_create_stdin(
            config_file=tmp_path / "c",
            password_file=password,
            stdin_file="vda.raw",
            tags={},
            source_stream=upstream,  # type: ignore[arg-type]
            override_source="h:libvirt-backup:uuid/vda",
            timeout=3,
        )
    assert info.value.result.returncode == TIMEOUT_RETURN_CODE
    assert "timed out after 3 seconds" in info.value.result.stderr
    assert upstream.terminated is True


def test_snapshot_create_stdin_times_out_upstream_wait(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")

    class HungUpstream(_Terminable):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired(self.args, timeout)

    upstream = HungUpstream()
    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaSuccess)
    with pytest.raises(kopia_snapshots.SnapshotCreateError) as info:
        kopia_snapshots.snapshot_create_stdin(
            config_file=tmp_path / "c",
            password_file=password,
            stdin_file="vda.raw",
            tags={},
            source_stream=upstream,  # type: ignore[arg-type]
            override_source="h:libvirt-backup:uuid/vda",
            timeout=2,
        )
    assert info.value.snapshot_id == "snap-stdin"
    assert info.value.result.returncode == TIMEOUT_RETURN_CODE
    assert upstream.terminated is True
