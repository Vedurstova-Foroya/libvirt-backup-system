"""Tests for uncovered error paths in ``libvirt_backup_system.restore``.

Covers:
- ``_inactive_domain_xml`` CommandError / OSError handling (lines 130-134)
- ``_define_domain_xml`` CommandError / OSError handling (lines 140-145)
- ``_restore_overwrite`` early exit when ``_inactive_domain_xml`` returns None (lines 218-219)
- ``_restore_overwrite`` early exit when ``_shutdown_and_undefine`` returns False (lines 222-223)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore
from libvirt_backup_system.manifest import MANIFEST_FILENAME, Manifest, ManifestDisk
from libvirt_backup_system.shell import CommandError, CommandResult

from .conftest import ALPHA_UUID
from .restore_helpers import TIMESTAMP, Snap, make_config, make_manifest, make_row, rows_result

# ---- _inactive_domain_xml: CommandError (lines 130-131) ----


def test_inactive_domain_xml_returns_none_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(args: list[str], **_: Any) -> CommandResult:
        raise CommandError(CommandResult(["virsh"], 1, "", "domain not found"))

    monkeypatch.setattr(restore, "run", fake_run)
    assert restore._inactive_domain_xml(make_config(tmp_path), "myvm") is None
    assert "dumpxml failed before restore" in capsys.readouterr().err


# ---- _inactive_domain_xml: OSError (lines 132-133) ----


def test_inactive_domain_xml_returns_none_on_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(args: list[str], **_: Any) -> CommandResult:
        raise OSError("virsh not found")

    monkeypatch.setattr(restore, "run", fake_run)
    assert restore._inactive_domain_xml(make_config(tmp_path), "myvm") is None
    assert "virsh dumpxml unavailable" in capsys.readouterr().err


# ---- _define_domain_xml: CommandError (lines 140-141) ----


def test_define_domain_xml_returns_false_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(args: list[str], **_: Any) -> CommandResult:
        raise CommandError(CommandResult(["virsh"], 1, "", "define failed"))

    monkeypatch.setattr(restore, "run", fake_run)
    xml_path = tmp_path / "domain.xml"
    xml_path.write_text("<domain/>", encoding="utf-8")
    assert restore._define_domain_xml(make_config(tmp_path), xml_path, log_context="test define") is False
    assert "test define failed" in capsys.readouterr().err


# ---- _define_domain_xml: OSError (lines 143-144) ----


def test_define_domain_xml_returns_false_on_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(args: list[str], **_: Any) -> CommandResult:
        raise OSError("virsh not found")

    monkeypatch.setattr(restore, "run", fake_run)
    xml_path = tmp_path / "domain.xml"
    xml_path.write_text("<domain/>", encoding="utf-8")
    assert restore._define_domain_xml(make_config(tmp_path), xml_path, log_context="test define") is False
    assert "test define unavailable" in capsys.readouterr().err


# ---- helpers for overwrite integration paths ----


def _install_meta_writer(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    def writer(**kwargs: Any) -> None:
        (kwargs["dest"] / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", writer)


def _install_disk_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [Snap("snap-vda")])


def _manifest_with_local_disks(tmp_path: Path) -> tuple[Manifest, Path]:
    src_dir = tmp_path / "imgs"
    src_dir.mkdir()
    manifest = make_manifest(
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


# ---- _restore_overwrite: _inactive_domain_xml returns None (lines 218-219) ----


def test_restore_overwrite_exits_when_dumpxml_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``_inactive_domain_xml`` returns None after disks are materialized,
    ``_restore_overwrite`` must clean up temp files and return 1."""
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, src_dir = _manifest_with_local_disks(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"new-disk")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "dumpxml" in args:
            raise CommandError(CommandResult(args, 1, "", "domain gone"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: pytest.fail("define must not be called"))
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    # Temp file should have been cleaned up.
    temp = src_dir / ".myvm-vda.qcow2.vda.restore.tmp"
    assert not temp.exists()


# ---- _restore_overwrite: _shutdown_and_undefine returns False (lines 222-223) ----


def test_restore_overwrite_exits_when_shutdown_fails_after_dumpxml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``_shutdown_and_undefine`` returns False (after dumpxml succeeded),
    ``_restore_overwrite`` must clean up temp files and return 1."""
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, src_dir = _manifest_with_local_disks(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups_result", lambda _c, *, vm_uuid=None: rows_result([row]))
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"new-disk")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "dumpxml" in args:
            return CommandResult(args, 0, "<domain><name>myvm</name></domain>", "")
        if "domstate" in args:
            # Return "running" so _shutdown_and_undefine returns False.
            return CommandResult(args, 0, "running\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: pytest.fail("define must not be called"))
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    # Temp file should have been cleaned up.
    temp = src_dir / ".myvm-vda.qcow2.vda.restore.tmp"
    assert not temp.exists()
