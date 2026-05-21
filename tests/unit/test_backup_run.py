"""Backup orchestrator tests: error paths, helpers, ``run_backups``.

Continues the ``test_backup`` coverage in a sibling file so each remains
under the project's 300-LOC ceiling. Re-uses the ``FakeSnapper`` /
``_install_stubs`` machinery so the orchestrator's failure branches are
exercised with the same fakes the happy-path tests use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import backup
from libvirt_backup_system import manifest as manifest_module
from libvirt_backup_system import shell as shell_module
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM

from .conftest import ALPHA_UUID, BETA_UUID
from .test_backup import FakeSnapper, _disk_entry, _disk_target, _install_stubs, _vm


def test_stream_disk_failure_returns_false_and_still_commits(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 7, "", "kopia exploded"))

    _install_stubs(monkeypatch, create_stdin=boom)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    # ``commit`` always runs (it's in the ``finally`` block) so the overlay
    # is folded back even when the disk snapshot bombs.
    assert snapper.commit_calls
    assert "disk snapshot failed" in capsys.readouterr().err


def test_missing_snapshot_base_logs_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # Manifest enumerates ``vda`` but the snapper's ``list_disks`` returns
    # ``vdb`` only — the join lookup in ``_stream_all_disks`` then yields
    # ``None`` for the base path and the run aborts.
    _install_stubs(monkeypatch, disks=[_disk_entry("vda", "/img/vda.qcow2")])
    snapper = FakeSnapper(disks=[_disk_target("vdb", "/img/vdb.qcow2")])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "missing snapshot base for disk" in capsys.readouterr().err
    assert snapper.commit_calls  # commit still runs to unwind overlays


def test_commit_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)
    commit_error = CommandError(CommandResult(["virsh"], 1, "", "blockcommit failed"))
    snapper = FakeSnapper(disks=[_disk_target()], commit_error=commit_error)
    # Disk snapshots succeed, but the commit-in-finally raises and the
    # ``_stream_all_disks`` wrapper translates that to a failed run.
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "snapshot commit failed" in capsys.readouterr().err


def test_meta_snapshot_failure_returns_false(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom_meta(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 9, "", "meta exploded"))

    _install_stubs(monkeypatch, create_path=boom_meta)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "meta snapshot failed" in capsys.readouterr().err


def test_meta_snapshot_returns_false_when_manifest_write_fails(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    _install_stubs(monkeypatch)

    def fail_write(self: Any, directory: Path, vm_name: str | None = None) -> bool:
        return False

    monkeypatch.setattr(manifest_module.Manifest, "write", fail_write)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False


def test_parallelism_handles_empty_and_invalid_env(backup_config: Config) -> None:
    backup_config.values["KOPIA_PARALLELISM"] = ""
    assert backup._parallelism(backup_config) is None
    backup_config.values["KOPIA_PARALLELISM"] = "   "
    assert backup._parallelism(backup_config) is None
    backup_config.values["KOPIA_PARALLELISM"] = "not-a-number"
    assert backup._parallelism(backup_config) is None
    backup_config.values["KOPIA_PARALLELISM"] = "8"
    assert backup._parallelism(backup_config) == 8


def test_parallelism_propagates_into_kopia_call(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    backup_config.values["KOPIA_PARALLELISM"] = "3"
    captured = _install_stubs(monkeypatch)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
    assert captured["create_stdin"][0]["parallelism"] == 3
    assert captured["create_path"][0]["parallelism"] == 3


def test_read_domain_xml_invokes_virsh_dumpxml(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        captured.append(args)
        return CommandResult(args, 0, "<domain/>", "")

    monkeypatch.setattr(shell_module, "run", fake_run)
    xml = backup._read_domain_xml("qemu:///system", "alpha")
    assert xml == "<domain/>"
    assert captured == [["virsh", "-c", "qemu:///system", "dumpxml", "--inactive", "--", "alpha"]]


def test_override_source_format(backup_config: Config) -> None:
    backup_config.values["HOST_ID"] = "host-a"
    assert backup._override_source(backup_config, ALPHA_UUID, "meta") == f"host-a:libvirt-backup:{ALPHA_UUID}/meta"


def test_disk_and_meta_tags(backup_config: Config) -> None:
    backup_config.values["HOST_ID"] = "host-a"
    vm = _vm(uuid=BETA_UUID)
    disk_tags = backup._disk_tags(backup_config, vm, "run-9", "vda")
    assert disk_tags == {"vm-uuid": BETA_UUID, "disk": "vda", "host": "host-a", "run-id": "run-9", "kind": "disk"}
    meta_tags = backup._meta_tags(backup_config, vm, "run-9")
    assert meta_tags == {"vm-uuid": BETA_UUID, "host": "host-a", "run-id": "run-9", "kind": "meta"}


def test_run_backups_runs_only_running_vms_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    invoked: list[str] = []

    def fake_list_vms(_config: Config) -> list[VM]:
        return [
            _vm("alpha", ALPHA_UUID, state="running"),
            _vm("beta", BETA_UUID, state="shut off"),
        ]

    def fake_backup_vm(_config: Config, vm: VM) -> bool:
        invoked.append(vm.name)
        return True

    monkeypatch.setattr(backup, "list_vms", fake_list_vms)
    monkeypatch.setattr(backup, "backup_vm", fake_backup_vm)
    assert backup.run_backups(backup_config) == 0
    assert invoked == ["alpha"]
    assert "skipping vm because it is offline" in capsys.readouterr().out


def test_run_backups_returns_one_on_any_failure(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    def fake_list_vms(_config: Config) -> list[VM]:
        return [
            _vm("alpha", ALPHA_UUID, state="running"),
            _vm("beta", BETA_UUID, state="running"),
        ]

    def fake_backup_vm(_config: Config, vm: VM) -> bool:
        return vm.name != "beta"

    monkeypatch.setattr(backup, "list_vms", fake_list_vms)
    monkeypatch.setattr(backup, "backup_vm", fake_backup_vm)
    assert backup.run_backups(backup_config) == 1
