from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import vm_snapshot
from libvirt_backup_system.shell import CommandError, CommandResult


def _make_snapper(tmp_path: Path) -> vm_snapshot.LibvirtSnapshotter:
    return vm_snapshot.LibvirtSnapshotter(
        libvirt_uri="qemu:///system",
        socket_root=tmp_path,
    )


def test_list_disks_filters_non_disk_devices(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stdout = (
        " Type   Device   Target   Source\n"
        "----------------------------------\n"
        " file   disk     vda      /var/lib/libvirt/images/alpha.qcow2\n"
        " file   cdrom    sda      /tmp/iso.iso\n"
        " file   disk     vdb      -\n"
        " file   disk     vdc      /var/lib/libvirt/images/extra disk.qcow2\n"
    )

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        assert "domblklist" in args
        return CommandResult(args, 0, stdout, "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    snap = _make_snapper(tmp_path)
    disks = snap.list_disks("alpha")
    assert [d.target for d in disks] == ["vda", "vdc"]
    # ``split(None, 3)`` preserves spaces in the path tail so qcow2 files in
    # directories named with embedded whitespace survive the parse intact.
    assert disks[1].source == Path("/var/lib/libvirt/images/extra disk.qcow2")


def test_freeze_attempts_quiesce_then_records_overlays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    snap = _make_snapper(tmp_path)
    disks = [
        vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2")),
        vm_snapshot.DiskTarget(target="vdb", source=Path("/img/vdb.qcow2")),
    ]
    result = snap.freeze("alpha", disks)
    assert result.quiesced is True
    assert "--quiesce" in calls[0]
    assert "--atomic" in calls[0]
    assert "--no-metadata" in calls[0]
    assert set(result.overlays) == {"vda", "vdb"}
    assert all(path.parent.is_dir() for path in result.overlays.values())


def test_freeze_retries_without_quiesce_on_qga_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    attempts: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        attempts.append(args)
        if "--quiesce" in args:
            raise CommandError(CommandResult(args, 1, "", "QGA not running"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    snap = _make_snapper(tmp_path)
    disks = [vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2"))]
    result = snap.freeze("alpha", disks)
    assert result.quiesced is False
    assert len(attempts) == 2
    assert "QGA quiesce failed" in capsys.readouterr().err


def test_commit_pivots_all_disks_and_unlinks_overlays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    snap = _make_snapper(tmp_path)
    overlay = tmp_path / "overlay.qcow2"
    overlay.write_text("ov", encoding="utf-8")
    disks = (vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2")),)
    frozen = vm_snapshot.FrozenSnapshot(
        vm_name="alpha", snapshot_name="snap-1", overlays={"vda": overlay}, bases=disks, quiesced=True
    )
    snap.commit(frozen)
    assert any("blockcommit" in args for args in calls)
    assert "--pivot" in calls[0]
    assert "--shallow" in calls[0]
    assert not overlay.exists()


def test_commit_removes_empty_runtime_dir_after_pivot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gap C: a clean run leaves no staging dir behind.

    ``freeze`` creates ``domain-libvirt-backup-<vm>/`` to hold each overlay
    so libvirt's dynamic AppArmor profile picks them up. After a successful
    blockcommit + unlink, the directory is empty and should be removed; if
    anything else (a wedged overlay, a libvirt-owned file) is still inside,
    ``rmdir`` fails harmlessly and the dir is left alone.
    """

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    runtime = tmp_path / "domain-libvirt-backup-alpha"
    runtime.mkdir()
    overlay = runtime / "vda.snap.overlay"
    overlay.write_text("ov", encoding="utf-8")
    snap = _make_snapper(tmp_path)
    disks = (vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2")),)
    frozen = vm_snapshot.FrozenSnapshot(
        vm_name="alpha", snapshot_name="snap-1", overlays={"vda": overlay}, bases=disks, quiesced=True
    )
    snap.commit(frozen)
    assert not overlay.exists()
    assert not runtime.exists()


def test_commit_leaves_runtime_dir_alone_when_not_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gap C edge case: a non-empty staging dir is never wiped.

    If a blockcommit on disk A failed and left ``vda.overlay`` in place but
    disk B's pivot succeeded, the rmdir attempt for ``vdb``'s unlink must
    not touch the surviving overlay. ``contextlib.suppress(OSError)`` swallows
    the ``ENOTEMPTY`` and leaves the directory for an operator to clean up.
    """

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    runtime = tmp_path / "domain-libvirt-backup-alpha"
    runtime.mkdir()
    overlay = runtime / "vda.snap.overlay"
    overlay.write_text("ov", encoding="utf-8")
    stray = runtime / "leftover.file"
    stray.write_text("stay", encoding="utf-8")
    snap = _make_snapper(tmp_path)
    disks = (vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2")),)
    frozen = vm_snapshot.FrozenSnapshot(
        vm_name="alpha", snapshot_name="snap-1", overlays={"vda": overlay}, bases=disks, quiesced=True
    )
    snap.commit(frozen)
    assert runtime.exists()
    assert stray.exists()


def test_commit_reraises_first_failure_but_continues_other_disks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    attempts: list[str] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        target = args[5] if "blockcommit" in args else ""
        if target == "vda":
            attempts.append(target)
            raise CommandError(CommandResult(args, 2, "", "still active"))
        attempts.append(target)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(vm_snapshot, "run", fake_run)
    snap = _make_snapper(tmp_path)
    disks = (
        vm_snapshot.DiskTarget(target="vda", source=Path("/img/vda.qcow2")),
        vm_snapshot.DiskTarget(target="vdb", source=Path("/img/vdb.qcow2")),
    )
    frozen = vm_snapshot.FrozenSnapshot(
        vm_name="alpha", snapshot_name="snap-1", overlays={}, bases=disks, quiesced=True
    )
    with pytest.raises(CommandError):
        snap.commit(frozen)
    assert attempts == ["vda", "vdb"]
