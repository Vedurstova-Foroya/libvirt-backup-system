"""End-to-end orchestration tests for ``libvirt_backup_system.restore``.

Drives the full ``restore()`` entry point through the overwrite and turnkey
branches. The per-helper tests are in ``test_restore.py`` /
``test_restore_helpers.py``; this file owns the multi-stage success and
failure flows.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore
from libvirt_backup_system.config import Config
from libvirt_backup_system.manifest import MANIFEST_FILENAME, Manifest, ManifestDisk
from libvirt_backup_system.restore_define import RESTORED_CONFIG_FILE
from libvirt_backup_system.shell import CommandError, CommandResult

from .conftest import ALPHA_UUID
from .restore_helpers import (
    TIMESTAMP,
    ConvertFail,
    ConvertOk,
    KopiaProc,
    Snap,
    make_config,
    make_manifest,
    make_row,
)


def _install_meta_writer(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    def writer(**kwargs: Any) -> None:
        (kwargs["dest"] / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", writer)


def _install_disk_snapshot(monkeypatch: pytest.MonkeyPatch, *, present: bool = True) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [Snap("snap-vda")] if present else [])


def _install_stream(monkeypatch: pytest.MonkeyPatch, *, popen: type) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_stdout", lambda **_: KopiaProc())
    monkeypatch.setattr(restore.subprocess, "Popen", popen)


def _record_stream_dests(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stub ``_stream_disk_to_qcow2`` so we can observe per-disk dest paths.

    Returned list is appended to in call order; each invocation also
    writes a stub byte so any preflight ``exists`` check sees real data.
    """
    streamed: list[Path] = []

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"\x00")
        streamed.append(dest)
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    return streamed


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


def test_restore_overwrite_path_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, src_dir = _manifest_with_local_disks(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    streamed = _record_stream_dests(monkeypatch)

    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        calls.append(args)
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)

    captured: dict[str, Any] = {}

    def fake_define(_cfg: Config, path: Path, vm_uuid: str, name: str | None) -> bool:
        captured["path"] = path
        captured["vm_uuid"] = vm_uuid
        captured["name"] = name
        captured["xml"] = path.read_text(encoding="utf-8")
        return True

    monkeypatch.setattr(restore, "define_restored_domain", fake_define)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP, verbose=True) == 0
    flat = {token for args in calls for token in args}
    assert {"destroy", "domstate", "undefine"}.issubset(flat)
    assert captured["name"] == "myvm"
    assert captured["vm_uuid"] == ALPHA_UUID
    # The disk MUST land at the manifest's original source_path, not
    # under the per-run staging dir keyed by target name. The previous
    # bug used ``dest_dir / f"{disk.target}.qcow2"`` which renamed
    # ``alpha-system.qcow2`` to ``vda.qcow2`` and broke the domain XML.
    expected_path = Path(manifest.disks[0].source_path)
    assert streamed == [expected_path]
    assert expected_path.parent == src_dir
    out = capsys.readouterr().out
    assert "restore overwrite completed" in out
    assert "restored disk" in out  # verbose=True branch
    # Overwrite passes the manifest XML through unchanged (name/uuid
    # rewrite is done downstream by ``define_restored_domain``).
    assert captured["xml"] == manifest.domain_xml


def test_restore_overwrite_writes_each_disk_to_its_own_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-pool layout: two disks under different parents must both land at home.

    Regression guard for the old ``_existing_disk_dir`` collapse where
    two different parents fell through to the staging dir.
    """
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    pool_a = tmp_path / "pool-a"
    pool_a.mkdir()
    pool_b = tmp_path / "pool-b"
    pool_b.mkdir()
    manifest = make_manifest(
        disks=(
            ManifestDisk(
                target="vda",
                source_path=str(pool_a / "alpha-system.qcow2"),
                virtual_size_bytes=4096,
                snapshot_filename="vda.raw",
            ),
            ManifestDisk(
                target="vdb",
                source_path=str(pool_b / "alpha-data.qcow2"),
                virtual_size_bytes=4096,
                snapshot_filename="vdb.raw",
            ),
        ),
    )
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    streamed = _record_stream_dests(monkeypatch)
    monkeypatch.setattr(
        restore,
        "run",
        lambda args, **_: CommandResult(args, 0, "myvm\n" if "domname" in args else "shut off\n", ""),
    )
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP, verbose=False) == 0
    assert streamed == [
        Path(manifest.disks[0].source_path),
        Path(manifest.disks[1].source_path),
    ]


def test_restore_turnkey_different_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path, host_id="host-a")
    row = make_row(tmp_path, host_id="host-b")  # different host -> turnkey
    # Build a manifest whose domain XML carries a real ``<disk>`` so we
    # can verify the source-path rewrite the turnkey branch performs.
    manifest = Manifest(
        vm_name="myvm",
        vm_uuid=ALPHA_UUID,
        host_id="host-b",
        run_id="run-1",
        timestamp=TIMESTAMP,
        libvirt_uri="qemu:///system",
        domain_xml=(
            "<domain type='kvm'>"
            "<name>myvm</name>"
            "<devices>"
            "<disk type='file' device='disk'>"
            "<source file='/var/lib/libvirt/images/myvm-vda.qcow2'/>"
            "<target dev='vda' bus='virtio'/>"
            "</disk>"
            "</devices>"
            "</domain>"
        ),
        disks=(
            ManifestDisk(
                target="vda",
                source_path="/var/lib/libvirt/images/myvm-vda.qcow2",
                virtual_size_bytes=4096,
                snapshot_filename="vda.raw",
            ),
        ),
    )
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    streamed = _record_stream_dests(monkeypatch)
    # Blank ``domname`` stdout drives the ``local_name is None`` branch.
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    captured: dict[str, Any] = {}

    def fake_define(_cfg: Config, path: Path, vm_uuid: str, name: str | None) -> bool:
        captured["path"] = path
        captured["name"] = name
        captured["xml"] = path.read_text(encoding="utf-8")
        return True

    monkeypatch.setattr(restore, "define_restored_domain", fake_define)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP, verbose=False) == 0
    assert captured["name"] == "myvm"
    assert captured["path"].name == RESTORED_CONFIG_FILE
    assert "restore turnkey completed" in capsys.readouterr().out

    # The disk lands under the per-run staging dir (the same dir that
    # holds the restored XML) rather than at the original source path
    # — important because the original ``/var/lib/libvirt/images``
    # parent might not even exist on the destination host.
    assert len(streamed) == 1
    assert streamed[0].name == "vda.qcow2"
    assert streamed[0].parent == captured["path"].parent

    # And the XML libvirt will ingest has its ``<source>`` rewritten to
    # match. Without this rewrite the defined domain would still
    # reference the source host's ``/var/lib/libvirt/images`` path,
    # which doesn't exist locally — exactly the gap-B bug.
    root = ET.fromstring(captured["xml"])
    source = root.find(".//devices/disk/source")
    assert source is not None
    assert source.get("file") == str(streamed[0])
    assert "/var/lib/libvirt/images" not in (source.get("file") or "")


def test_restore_overwrite_shutdown_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest = make_manifest()
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
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
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
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
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, make_manifest(host_id="host-b"))
    _install_disk_snapshot(monkeypatch, present=False)
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_turnkey_qemu_convert_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path, host_id="host-b")
    manifest, _ = _manifest_with_local_disks(tmp_path, host_id="host-b")
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
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
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    _install_stream(monkeypatch, popen=ConvertOk)
    monkeypatch.setattr(restore, "run", lambda args, **_: CommandResult(args, 0, "", ""))
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: False)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_overwrite_define_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    manifest, _ = _manifest_with_local_disks(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, manifest)
    _install_disk_snapshot(monkeypatch)
    _install_stream(monkeypatch, popen=ConvertOk)

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: False)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_overwrite_disk_materialize_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    row = make_row(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [row])
    _install_meta_writer(monkeypatch, make_manifest())
    _install_disk_snapshot(monkeypatch, present=False)

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, "myvm\n", "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off\n", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(restore, "run", fake_run)
    monkeypatch.setattr(restore, "define_restored_domain", lambda *_a, **_kw: True)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
