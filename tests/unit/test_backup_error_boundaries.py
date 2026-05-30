from __future__ import annotations

import pytest

from libvirt_backup_system import backup
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vm_snapshot import DiskTarget

from .test_backup import FakeSnapper, _disk_target, _find_event, _install_stubs, _vm


def test_list_disks_command_error_returns_failed_vm_result(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)

    class ListFailSnapper(FakeSnapper):
        def list_disks(self, vm_name: str) -> list[DiskTarget]:
            raise CommandError(CommandResult(["virsh", "domblklist"], 1, "", "libvirt unavailable"))

    assert backup.backup_vm(backup_config, _vm(), snapper=ListFailSnapper(disks=[])) is False
    record = _find_event(capsys.readouterr().err, "disk listing failed")
    assert record["vm"] == "alpha"
    assert record["stderr"] == "libvirt unavailable"


def test_domain_xml_command_error_returns_failed_vm_result(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)

    def boom_xml(_libvirt_uri: str, _vm_name: str) -> str:
        raise CommandError(CommandResult(["virsh", "dumpxml"], 1, "", "dumpxml failed"))

    monkeypatch.setattr(backup, "_read_domain_xml", boom_xml)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    record = _find_event(capsys.readouterr().err, "domain xml read failed")
    assert record["vm"] == "alpha"
    assert record["stderr"] == "dumpxml failed"
    assert snapper.freeze_calls == []
