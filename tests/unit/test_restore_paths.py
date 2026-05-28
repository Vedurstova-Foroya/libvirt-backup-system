"""End-to-end orchestration tests for ``libvirt_backup_system.restore``.

Drives the full ``restore()`` entry point through the overwrite and turnkey
branches. The per-helper tests are in ``test_restore.py`` /
``test_restore_helpers.py``; this file owns the multi-stage success and
failure flows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore, restore_io
from libvirt_backup_system.manifest import MANIFEST_FILENAME, Manifest, ManifestDisk
from libvirt_backup_system.shell import CommandError, CommandResult

from .conftest import ALPHA_UUID
from .restore_helpers import (
    TIMESTAMP,
    ConvertFail,
    KopiaProc,
    Snap,
    make_config,
    make_manifest,
    make_row,
    rows_result,
)


def _install_meta_writer(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    def writer(**kwargs: Any) -> None:
        (kwargs["dest"] / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", writer)


def _install_disk_snapshot(monkeypatch: pytest.MonkeyPatch, *, present: bool = True) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [Snap("snap-vda")] if present else [])


def _install_stream(monkeypatch: pytest.MonkeyPatch, *, popen: type) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_stdout", lambda **_: KopiaProc())
    monkeypatch.setattr(restore_io.subprocess, "Popen", popen)


def _manifest_with_local_disks(tmp_path: Path, host_id: str = "host-a") -> tuple[Manifest, Path]:
    src_dir = tmp_path / "imgs"
    src_dir.mkdir()
    manifest = make_manifest(
        host_id=host_id,
        disks=(
            ManifestDisk(
                target="vda",
                source_path=str(src_dir / "myvm-vda.qcow2"),
                virtual_size_bytes=4096,
                snapshot_filename="vda.raw",
            ),
        ),
    )
    return manifest, src_dir


def test_restore_overwrite_shutdown_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest = make_manifest()
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            # Refuse to shut down -> shutdown_and_undefine returns False.
            return CommandResult(args, 0, "running\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_overwrite_undefine_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, make_manifest())

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        if "undefine" in args:
            raise CommandError(CommandResult(args, 1, "", "boom"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_turnkey_disk_snapshot_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path, host_id="host-b")
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, make_manifest(host_id="host-b"))
    _install_disk_snapshot(monkeypatch, present=False)
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_turnkey_qemu_convert_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path, host_id="host-b")
    manifest, _ = _manifest_with_local_disks(tmp_path, host_id="host-b")
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    _install_stream(monkeypatch, popen=ConvertFail)
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_turnkey_define_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path, host_id="host-b")
    manifest, _ = _manifest_with_local_disks(tmp_path, host_id="host-b")
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"new-disk")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: False)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_overwrite_define_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, src_dir = _manifest_with_local_disks(tmp_path)
    original_disk = src_dir / "myvm-vda.qcow2"
    original_disk.write_bytes(b"old-disk")
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"new-disk")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    original_xml = "<domain><name>myvm</name><uuid>old</uuid></domain>"
    defined_paths: list[Path] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "dumpxml" in args:
            return CommandResult(args, 0, original_xml, "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        if "define" in args:
            defined_paths.append(Path(args[-1]))
            return CommandResult(args, 0, "", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    define_called = False

    def fail_define(*_a: Any, **_kw: Any) -> bool:
        nonlocal define_called
        define_called = True
        assert original_disk.read_bytes() == b"new-disk"
        return False

    monkeypatch.setattr(restore, "define_restored_domain", fail_define)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    assert define_called is True
    assert original_disk.read_bytes() == b"old-disk"
    assert len(defined_paths) == 1
    assert defined_paths[0].read_text(encoding="utf-8") == original_xml
    assert not (src_dir / ".myvm-vda.qcow2.vda.restore.old").exists()


def test_restore_overwrite_replace_failure_redefines_original_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, src_dir = _manifest_with_local_disks(tmp_path)
    original_disk = src_dir / "myvm-vda.qcow2"
    original_disk.write_bytes(b"old-disk")
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"new-disk")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    original_xml = "<domain><name>myvm</name><uuid>old</uuid></domain>"
    defined_paths: list[Path] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "dumpxml" in args:
            return CommandResult(args, 0, original_xml, "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        if "define" in args:
            defined_paths.append(Path(args[-1]))
            return CommandResult(args, 0, "", "")
        return CommandResult(args, 0, "", "")

    original_replace = Path.replace

    def fail_temp_replace(self: Path, target: Path) -> Path:
        if self.name.endswith(".restore.tmp") and target == original_disk:
            raise OSError("replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(Path, "replace", fail_temp_replace)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    assert original_disk.read_bytes() == b"old-disk"
    assert len(defined_paths) == 1
    assert defined_paths[0].read_text(encoding="utf-8") == original_xml


def test_restore_overwrite_disk_materialize_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, make_manifest())
    _install_disk_snapshot(monkeypatch, present=False)

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        calls.append(args)
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    assert not any("destroy" in args or "undefine" in args for args in calls)
