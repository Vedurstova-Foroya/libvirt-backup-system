from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import restore
from libvirt_backup_system.manifest import ManifestDisk

from .restore_helpers import make_config, make_manifest, make_row


def test_overwrite_dest_map_uses_original_source_paths() -> None:
    disks = (
        ManifestDisk(
            target="vda",
            source_path="/pool-a/alpha-system.qcow2",
            virtual_size_bytes=1,
            snapshot_filename="vda.raw",
        ),
        ManifestDisk(
            target="vdb",
            source_path="/pool-b/alpha-data.qcow2",
            virtual_size_bytes=1,
            snapshot_filename="vdb.raw",
        ),
    )
    dest_map = restore._overwrite_dest_map(make_manifest(disks=disks))
    assert dest_map == {
        "vda": Path("/pool-a/alpha-system.qcow2"),
        "vdb": Path("/pool-b/alpha-data.qcow2"),
    }


def test_turnkey_dest_map_lands_under_staging(tmp_path: Path) -> None:
    """Turnkey map sticks every disk under the staging dir as ``<target>.qcow2``."""
    staging = tmp_path / "stage"
    disks = (
        ManifestDisk(target="vda", source_path="/pool-a/x.qcow2", virtual_size_bytes=1, snapshot_filename="vda.raw"),
        ManifestDisk(target="vdb", source_path="/pool-b/y.qcow2", virtual_size_bytes=1, snapshot_filename="vdb.raw"),
    )
    dest_map = restore._turnkey_dest_map(make_manifest(disks=disks), staging)
    assert dest_map == {"vda": staging / "vda.qcow2", "vdb": staging / "vdb.qcow2"}


@pytest.mark.parametrize("target", ["vda/../../escape", "../escape", "/escape", "/var/lib/disk", ".."])
def test_turnkey_dest_map_sanitizes_malformed_targets(tmp_path: Path, target: str) -> None:
    staging = tmp_path / "stage"
    disk = ManifestDisk(target=target, source_path="/pool/x.qcow2", virtual_size_bytes=1, snapshot_filename="x.raw")
    dest = restore._turnkey_dest_map(make_manifest(disks=(disk,)), staging)[target]
    relative = dest.relative_to(staging)
    assert dest.parent == staging
    assert ".." not in relative.parts
    assert dest.name.endswith(".qcow2")


def test_overwrite_temp_dest_map_uses_sibling_hidden_paths(tmp_path: Path) -> None:
    dest_map = {"vda": tmp_path / "pool-a" / "sys.qcow2"}
    temp_map = restore._overwrite_temp_dest_map(dest_map)
    assert temp_map == {"vda": tmp_path / "pool-a" / ".sys.qcow2.vda.restore.tmp"}


def test_replace_overwrite_disks_moves_temp_to_final(tmp_path: Path) -> None:
    dest = tmp_path / "disk.qcow2"
    dest.write_bytes(b"old")
    temp = tmp_path / ".disk.qcow2.vda.restore.tmp"
    temp.write_bytes(b"new")
    assert restore._replace_overwrite_disks({"vda": temp}, {"vda": dest}) is True
    assert dest.read_bytes() == b"new"
    assert not temp.exists()
    assert not (tmp_path / ".disk.qcow2.vda.restore.old").exists()


def test_replace_overwrite_disks_rolls_back_after_later_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.qcow2"
    second = tmp_path / "second.qcow2"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    first_temp = tmp_path / ".first.qcow2.vda.restore.tmp"
    second_temp = tmp_path / ".second.qcow2.vdb.restore.tmp"
    first_temp.write_bytes(b"new-first")
    second_temp.write_bytes(b"new-second")
    original_replace = Path.replace

    def fail_second_temp_replace(self: Path, target: Path) -> Path:
        if self == second_temp and target == second:
            raise OSError("second replace failed")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_second_temp_replace)
    assert (
        restore._replace_overwrite_disks(
            {"vda": first_temp, "vdb": second_temp},
            {"vda": first, "vdb": second},
        )
        is False
    )
    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
    assert second_temp.read_bytes() == b"new-second"


def test_materialize_disks_writes_to_dest_map_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_materialize_disks`` must honor caller-supplied destinations per disk.

    Stubs the snapshot lookup + stream step so we can observe which paths
    actually get written. Two disks land in two different parent dirs (the
    overwrite-with-multi-pool case).
    """
    pool_a = tmp_path / "pool-a"
    pool_b = tmp_path / "pool-b"
    disks = (
        ManifestDisk(
            target="vda",
            source_path=str(pool_a / "sys.qcow2"),
            virtual_size_bytes=1,
            snapshot_filename="vda.raw",
        ),
        ManifestDisk(
            target="vdb",
            source_path=str(pool_b / "data.qcow2"),
            virtual_size_bytes=1,
            snapshot_filename="vdb.raw",
        ),
    )
    manifest = make_manifest(disks=disks)
    monkeypatch.setattr(restore, "_disk_snapshot_id", lambda *_a, **_kw: "snap-id")
    written: list[Path] = []

    def fake_stream(_cfg: Any, _row: Any, _sid: str, _file: str, dest: Path) -> bool:
        dest.write_bytes(b"\x00" * 8)
        written.append(dest)
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    ctx = restore._RestoreContext(row=make_row(tmp_path), manifest=manifest, staging=tmp_path / "s", verbose=False)
    assert restore._materialize_disks(ctx, make_config(tmp_path), restore._overwrite_dest_map(manifest)) is True
    assert written == [pool_a / "sys.qcow2", pool_b / "data.qcow2"]


def test_materialize_disks_unlinks_existing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-existing files at the destination are removed before ``qemu-img convert``.

    Otherwise stale chmod / ACLs / sparse holes could survive. We assert
    by recording whether the file was present when the stream stub ran.
    """
    pool = tmp_path / "imgs"
    pool.mkdir()
    target = pool / "sys.qcow2"
    target.write_bytes(b"stale")
    disks = (ManifestDisk(target="vda", source_path=str(target), virtual_size_bytes=1, snapshot_filename="vda.raw"),)
    manifest = make_manifest(disks=disks)
    monkeypatch.setattr(restore, "_disk_snapshot_id", lambda *_a, **_kw: "snap-id")
    observed: dict[str, bool] = {}

    def fake_stream(_cfg: Any, _row: Any, _sid: str, _file: str, dest: Path) -> bool:
        observed["existed_before_stream"] = dest.exists()
        dest.write_bytes(b"\x00")
        return True

    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", fake_stream)
    ctx = restore._RestoreContext(row=make_row(tmp_path), manifest=manifest, staging=tmp_path / "s", verbose=False)
    assert restore._materialize_disks(ctx, make_config(tmp_path), restore._overwrite_dest_map(manifest)) is True
    assert observed["existed_before_stream"] is False


def test_materialize_disks_returns_false_on_missing_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore, "_disk_snapshot_id", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        restore, "_stream_disk_to_qcow2", lambda *_a, **_kw: pytest.fail("stream must not run after lookup miss")
    )
    manifest = make_manifest()
    ctx = restore._RestoreContext(row=make_row(tmp_path), manifest=manifest, staging=tmp_path / "s", verbose=False)
    assert restore._materialize_disks(ctx, make_config(tmp_path), restore._overwrite_dest_map(manifest)) is False


def test_materialize_disks_returns_false_on_stream_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(restore, "_disk_snapshot_id", lambda *_a, **_kw: "snap-id")
    monkeypatch.setattr(restore, "_stream_disk_to_qcow2", lambda *_a, **_kw: False)
    manifest = make_manifest()
    ctx = restore._RestoreContext(row=make_row(tmp_path), manifest=manifest, staging=tmp_path / "s", verbose=False)
    assert restore._materialize_disks(ctx, make_config(tmp_path), restore._overwrite_dest_map(manifest)) is False


def test_rewrite_domain_disk_sources_rewrites_file_attr() -> None:
    xml = (
        "<domain type='kvm'>"
        "  <devices>"
        "    <disk type='file' device='disk'>"
        "      <source file='/old/path.qcow2'/>"
        "      <target dev='vda' bus='virtio'/>"
        "    </disk>"
        "  </devices>"
        "</domain>"
    )
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/new/restored.qcow2")})
    root = ET.fromstring(rewritten)
    source = root.find(".//devices/disk/source")
    assert source is not None
    assert source.get("file") == "/new/restored.qcow2"


def test_rewrite_domain_disk_sources_rewrites_dev_attr() -> None:
    xml = (
        "<domain type='kvm'><devices>"
        "<disk type='block' device='disk'>"
        "<source dev='/dev/old'/>"
        "<target dev='vdb' bus='virtio'/>"
        "</disk>"
        "</devices></domain>"
    )
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vdb": Path("/stage/vdb.qcow2")})
    root = ET.fromstring(rewritten)
    source = root.find(".//devices/disk/source")
    assert source is not None
    assert source.get("dev") == "/stage/vdb.qcow2"
    assert "file" not in source.attrib


def test_rewrite_domain_disk_sources_skips_disks_not_in_map() -> None:
    """A disk whose target dev is absent from the map keeps its old source path.

    This is defensive: the manifest can carry CDROM / passthrough disks
    that we never snapshot, so we should never accidentally relocate them.
    """
    xml = (
        "<domain><devices>"
        "<disk type='file'><source file='/keep/me.iso'/><target dev='hda'/></disk>"
        "<disk type='file'><source file='/old.qcow2'/><target dev='vda'/></disk>"
        "</devices></domain>"
    )
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/new.qcow2")})
    root = ET.fromstring(rewritten)
    targets = root.findall(".//disk/target")
    srcs = root.findall(".//disk/source")
    sources = {t.get("dev"): s for t, s in zip(targets, srcs, strict=True)}
    assert sources["hda"].get("file") == "/keep/me.iso"
    assert sources["vda"].get("file") == "/new.qcow2"


def test_rewrite_domain_disk_sources_skips_disks_without_target() -> None:
    """Disks without a ``<target>`` element are left alone (defensive)."""
    xml = "<domain><devices><disk><source file='/x'/></disk></devices></domain>"
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/y")})
    root = ET.fromstring(rewritten)
    source = root.find(".//disk/source")
    assert source is not None
    assert source.get("file") == "/x"


def test_rewrite_domain_disk_sources_skips_disks_without_target_dev() -> None:
    """``<target>`` with no ``dev=`` attr is unusable as a key, so we skip it."""
    xml = "<domain><devices><disk><target bus='virtio'/><source file='/x'/></disk></devices></domain>"
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/y")})
    root = ET.fromstring(rewritten)
    source = root.find(".//disk/source")
    assert source is not None
    assert source.get("file") == "/x"


def test_rewrite_domain_disk_sources_skips_disks_without_source_element() -> None:
    """A target-only disk (no ``<source>``) is left untouched."""
    xml = "<domain><devices><disk><target dev='vda'/></disk></devices></domain>"
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/y")})
    root = ET.fromstring(rewritten)
    disk = root.find(".//disk")
    assert disk is not None
    assert disk.find("source") is None


def test_rewrite_domain_disk_sources_skips_unknown_source_form() -> None:
    """A ``<source>`` element with neither ``file=`` nor ``dev=`` is left alone.

    Volume / network sources go through other libvirt code paths; we make
    no claim about restoring them and so should not mangle them.
    """
    xml = (
        "<domain><devices>"
        "<disk type='volume'><target dev='vda'/>"
        "<source pool='p' volume='v'/>"
        "</disk>"
        "</devices></domain>"
    )
    rewritten = restore._rewrite_domain_disk_sources(xml, {"vda": Path("/y")})
    root = ET.fromstring(rewritten)
    source = root.find(".//disk/source")
    assert source is not None
    assert source.get("pool") == "p"
    assert source.get("volume") == "v"
    assert "file" not in source.attrib
    assert "dev" not in source.attrib
