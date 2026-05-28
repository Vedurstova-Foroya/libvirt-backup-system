"""Shared fixtures for the ``restore`` test files.

Split out so the three ``test_restore*.py`` files stay under the project's
300-LOC ceiling without duplicating the same Manifest / BackupRow / Config
construction boilerplate. Importable from the ``tests.unit`` package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import BackupRow
from libvirt_backup_system.manifest import Manifest, ManifestDisk
from libvirt_backup_system.shell import CommandResult

from .conftest import ALPHA_UUID

TIMESTAMP = "20260101T010101"


def make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": host_id,
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    (tmp_path / "backups").mkdir(parents=True, exist_ok=True)
    return cfg


def make_manifest(
    *,
    vm_name: str = "myvm",
    host_id: str = "host-a",
    disks: tuple[ManifestDisk, ...] | None = None,
) -> Manifest:
    if disks is None:
        disks = (
            ManifestDisk(
                target="vda",
                source_path="/var/lib/libvirt/images/myvm-vda.qcow2",
                virtual_size_bytes=4096,
                snapshot_filename="vda.raw",
            ),
        )
    return Manifest(
        vm_name=vm_name,
        vm_uuid=ALPHA_UUID,
        host_id=host_id,
        run_id="run-1",
        timestamp=TIMESTAMP,
        libvirt_uri="qemu:///system",
        domain_xml="<domain type='kvm'><name>myvm</name></domain>",
        disks=disks,
    )


def make_row(tmp_path: Path, *, host_id: str = "host-a", run_id: str = "run-1") -> BackupRow:
    return BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp=TIMESTAMP,
        host_id=host_id,
        vm_name="",
        run_id=run_id,
        snapshot_id="abc123",
        config_file=tmp_path / "kopia.config",
    )


def ok_result(args: list[str], stdout: str = "") -> CommandResult:
    return CommandResult(args, 0, stdout, "")


class Snap:
    def __init__(self, snapshot_id: str) -> None:
        self.snapshot_id = snapshot_id


class KopiaProc:
    """Stand-in for the ``kopia restore -`` Popen object."""

    def __init__(self, *, returncode: int = 0, with_stdout: bool = True) -> None:
        self.returncode = returncode
        self.waited = False

        class _Stdout:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        self.stdout: Any = _Stdout() if with_stdout else None

    def wait(self) -> None:
        self.waited = True


class ConvertOk:
    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.returncode = 0

    def communicate(self) -> tuple[bytes, bytes]:
        return (b"", b"")


class ConvertFail:
    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.returncode = 9

    def communicate(self) -> tuple[bytes, bytes]:
        return (b"", b"qemu boom")


class ConvertWith:
    """Configurable ``qemu-img convert`` stub for the stream tests."""

    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self._rc = returncode
        self._stderr = stderr

    def __call__(self, *_a: Any, **_kw: Any) -> ConvertWith:
        self.returncode = self._rc
        return self

    def communicate(self) -> tuple[bytes, bytes]:
        return (b"", self._stderr)


def run_stream(
    restore_module: Any,
    tmp_path: Path,
    monkeypatch: Any,
    *,
    kopia: KopiaProc,
    popen: Any,
) -> bool:
    """Wire up ``_stream_disk_to_qcow2`` with stub kopia + qemu processes."""
    from libvirt_backup_system import kopia_snapshots, restore_io

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_stdout", lambda **_: kopia)
    monkeypatch.setattr(restore_io.subprocess, "Popen", popen)
    return restore_module._stream_disk_to_qcow2(
        make_config(tmp_path), make_row(tmp_path), "s", "vda.raw", tmp_path / "vda.qcow2"
    )


def run_shutdown(
    restore_module: Any,
    monkeypatch: Any,
    tmp_path: Path,
    *,
    side: dict[str, Any],
) -> bool:
    """Drive ``_shutdown_and_undefine`` with per-virsh-verb side effects."""

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        for verb, effect in side.items():
            if verb in args:
                if isinstance(effect, BaseException):
                    raise effect
                if callable(effect):
                    return effect(args)
        return ok_result(args)

    monkeypatch.setattr(restore_module, "run", fake_run)
    return restore_module._shutdown_and_undefine(make_config(tmp_path), "myvm")
