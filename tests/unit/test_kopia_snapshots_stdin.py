"""Stdin-piped ``snapshot create`` tests.

Pulled out of ``test_kopia_snapshots.py`` so each test file stays under the
project's 300-LOC ceiling. Shares the fake Popen pattern; ``kopia_snapshots``
is the module under test for both files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots
from libvirt_backup_system.shell import CommandError


def _write_password(path: Path, value: str = "swordfish") -> Path:
    path.write_text(value, encoding="utf-8")
    return path


class _KopiaSuccess:
    def __init__(self, args: list[str], **_: Any) -> None:
        self.args = args
        self.returncode = 0

    def communicate(self, _input: str | None = None) -> tuple[str, str]:
        return ("", "")


class _KopiaFailure:
    def __init__(self, args: list[str], **_: Any) -> None:
        self.args = args
        self.returncode = 5

    def communicate(self, _input: str | None = None) -> tuple[str, str]:
        return ("", "fail")


def test_snapshot_create_stdin_handles_none_source_stream(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaSuccess)
    kopia_snapshots.snapshot_create_stdin(
        config_file=tmp_path / "c",
        password_file=password,
        stdin_file="vda.raw",
        tags={},
        source_stream=None,
        override_source="h:libvirt-backup:uuid/vda",
    )


def test_snapshot_create_stdin_closes_upstream_stdout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    closed: list[bool] = []

    class FakeStdout:
        def close(self) -> None:
            closed.append(True)

    class FakeUpstream:
        args = ["qemu-nbd"]

        def __init__(self) -> None:
            self.stdout: FakeStdout | None = FakeStdout()
            self.returncode = 0

        def wait(self) -> None:
            return None

    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaSuccess)
    kopia_snapshots.snapshot_create_stdin(
        config_file=tmp_path / "c",
        password_file=password,
        stdin_file="vda.raw",
        tags={},
        source_stream=FakeUpstream(),  # type: ignore[arg-type]
        override_source="h:libvirt-backup:uuid/vda",
    )
    assert closed == [True]


def test_snapshot_create_stdin_handles_non_list_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")

    class FakeUpstream:
        args = "qemu-nbd -r"

        def __init__(self) -> None:
            self.stdout: object = None
            self.returncode = 11

        def wait(self) -> None:
            return None

    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaSuccess)
    with pytest.raises(CommandError) as info:
        kopia_snapshots.snapshot_create_stdin(
            config_file=tmp_path / "c",
            password_file=password,
            stdin_file="vda.raw",
            tags={},
            source_stream=FakeUpstream(),  # type: ignore[arg-type]
            override_source="h:libvirt-backup:uuid/vda",
        )
    assert info.value.result.returncode == 11
    assert info.value.result.args == ["qemu-nbd -r"]


def test_snapshot_create_stdin_propagates_upstream_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")

    class FakeUpstream:
        args = ["qemu-nbd", "-r"]

        def __init__(self) -> None:
            self.stdout: object = None
            self.returncode = 9

        def wait(self) -> None:
            return None

    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaSuccess)
    with pytest.raises(CommandError) as info:
        kopia_snapshots.snapshot_create_stdin(
            config_file=tmp_path / "c",
            password_file=password,
            stdin_file="vda.raw",
            tags={"x": "y"},
            source_stream=FakeUpstream(),  # type: ignore[arg-type]
            override_source="h:libvirt-backup:uuid/vda",
        )
    assert info.value.result.returncode == 9


def test_snapshot_create_stdin_propagates_kopia_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", _KopiaFailure)
    with pytest.raises(CommandError) as info:
        kopia_snapshots.snapshot_create_stdin(
            config_file=tmp_path / "c",
            password_file=password,
            stdin_file="vda.raw",
            tags={},
            source_stream=None,
            override_source="h:libvirt-backup:uuid/vda",
            parallelism=2,
        )
    assert info.value.result.returncode == 5


def test_snapshot_restore_to_stdout_spawns_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    captured: list[list[str]] = []

    class Proc:
        def __init__(self, args: list[str], **_: Any) -> None:
            captured.append(args)
            self.returncode = 0

    monkeypatch.setattr(kopia_snapshots.subprocess, "Popen", Proc)
    kopia_snapshots.snapshot_restore_to_stdout(
        config_file=tmp_path / "c",
        password_file=password,
        snapshot_id="abc",
        file_in_snapshot="vda.raw",
    )
    args = captured[0]
    assert args[-1] == "-"
    assert "abc/vda.raw" in args
