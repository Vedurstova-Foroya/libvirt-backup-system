"""Successful overwrite and turnkey restore orchestration tests."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore
from libvirt_backup_system.config import Config
from libvirt_backup_system.manifest import MANIFEST_FILENAME, Manifest, ManifestDisk
from libvirt_backup_system.restore_define import RESTORED_CONFIG_FILE
from libvirt_backup_system.shell import CommandResult

from .conftest import ALPHA_UUID
from .restore_helpers import TIMESTAMP, Snap, make_config, make_manifest, make_row


def _install_meta_writer(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    def writer(**kwargs: Any) -> None:
        (kwargs["dest"] / MANIFEST_FILENAME).write_text(manifest.to_json(), encoding="utf-8")

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", writer)


def _install_disk_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [Snap("snap-vda")])


def _record_stream_dests(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Stub ``_stream_disk_to_qcow2`` so we can observe per-disk dest paths."""
    streamed: list[Path] = []

    def fake_stream(_cfg: Any, _row: Any, _snap: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"\x00")
        streamed.append(dest)
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    return streamed


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
    expected_path = Path(manifest.disks[0].source_path)
    assert streamed == [src_dir / ".myvm-vda.qcow2.vda.restore.tmp"]
    assert expected_path.read_bytes() == b"\x00"
    out = capsys.readouterr().out
    assert "restore overwrite completed" in out
    assert "restored disk" in out
    assert captured["xml"] == manifest.domain_xml


def test_restore_overwrite_writes_each_disk_to_its_own_source_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multi-pool layout: two disks under different parents must both land at home."""
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
        Path(manifest.disks[0].source_path).with_name(".alpha-system.qcow2.vda.restore.tmp"),
        Path(manifest.disks[1].source_path).with_name(".alpha-data.qcow2.vdb.restore.tmp"),
    ]
    assert Path(manifest.disks[0].source_path).read_bytes() == b"\x00"
    assert Path(manifest.disks[1].source_path).read_bytes() == b"\x00"


def test_restore_turnkey_different_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path, host_id="host-a")
    row = make_row(tmp_path, host_id="host-b")
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
    assert len(streamed) == 1
    assert streamed[0].name == "vda.qcow2"
    assert streamed[0].parent == captured["path"].parent

    root = ET.fromstring(captured["xml"])
    source = root.find(".//devices/disk/source")
    assert source is not None
    assert source.get("file") == str(streamed[0])
    assert "/var/lib/libvirt/images" not in (source.get("file") or "")
