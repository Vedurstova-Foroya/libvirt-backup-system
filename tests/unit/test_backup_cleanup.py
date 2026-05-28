"""Regression tests for failed-run disk snapshot cleanup."""

from __future__ import annotations

from typing import Any

import pytest

from libvirt_backup_system import backup, kopia_snapshots
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult

from .test_backup import FakeSnapper, _disk_target, _install_stubs, _vm


def test_later_disk_failure_deletes_earlier_disk_snapshot(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    def create_stdin(**kwargs: Any) -> str:
        if kwargs["stdin_file"] == "vdb.raw":
            raise CommandError(CommandResult(["kopia"], 7, "", "second disk failed"))
        return "snap-vda"

    captured = _install_stubs(monkeypatch, create_stdin=create_stdin)
    snapper = FakeSnapper(disks=[_disk_target("vda", "/img/vda.qcow2"), _disk_target("vdb", "/img/vdb.qcow2")])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert [call["snapshot_id"] for call in captured["delete"]] == ["snap-vda"]


def test_meta_failure_deletes_created_disk_snapshot(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    def boom_meta(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 9, "", "meta exploded"))

    captured = _install_stubs(monkeypatch, create_path=boom_meta)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert [call["snapshot_id"] for call in captured["delete"]] == ["snap-vda.raw"]


def test_partial_disk_create_error_deletes_reported_snapshot(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    def partial_failure(**_: Any) -> None:
        raise kopia_snapshots.SnapshotCreateError(
            CommandResult(["nbdcopy"], 9, "", "upstream failed"), snapshot_id="snap-partial"
        )

    captured = _install_stubs(monkeypatch, create_stdin=partial_failure)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert [call["snapshot_id"] for call in captured["delete"]] == ["snap-partial"]


def test_post_meta_mount_loss_keeps_snapshots(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    captured = _install_stubs(monkeypatch, mount_ok=[True, False])
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert captured["delete"] == []
