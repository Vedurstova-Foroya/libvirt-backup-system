"""Backup orchestrator tests: error paths, helpers, ``run_backups``.

Continues the ``test_backup`` coverage in a sibling file so each remains
under the project's 300-LOC ceiling. Re-uses the ``FakeSnapper`` /
``_install_stubs`` machinery so the orchestrator's failure branches are
exercised with the same fakes the happy-path tests use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import backup
from libvirt_backup_system import manifest as manifest_module
from libvirt_backup_system import shell as shell_module
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vm_snapshot import DiskTarget, FrozenSnapshot
from libvirt_backup_system.vms import VM

from .conftest import ALPHA_UUID, BETA_UUID
from .test_backup import FakeSnapper, _disk_target, _find_event, _install_stubs, _vm


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


def test_backup_vm_completion_event_marks_crash_consistent_when_quiesce_fails(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gap B: the per-VM success event carries the quiesce outcome.

    When ``freeze`` falls back to a crash-consistent snapshot (QGA not
    answering, no guest agent installed, etc.) the ``backup completed``
    event must surface that fact so operators can grep run logs and tell
    which VMs need a guest-agent fix before the next backup window.
    """
    captured = _install_stubs(monkeypatch)
    snapper = FakeSnapper(disks=[_disk_target()], quiesced=False)
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
    completion = _find_event(capsys.readouterr().out, "backup completed")
    assert completion["consistency"] == "crash"
    assert json.loads(captured["create_path"][0]["manifest_json"])["consistency"] == "crash"
    assert captured["create_path"][0]["tags"]["consistency"] == "crash"


def test_backup_vm_runs_commit_even_when_streaming_fails(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gap D: ``commit`` must run when ``stream_disk`` raises mid-yield.

    The orchestrator's contract is that no overlay is ever left wedged on a
    running domain — even if the streaming context manager dies inside the
    ``with`` block, the surrounding ``try/finally`` in ``_stream_all_disks``
    has to fold the overlay back in. The ``FakeSnapper.stream_disk`` raises
    a ``CommandError`` *after* yielding, mirroring how a real
    ``qemu-nbd``/``nbdcopy`` pipeline dies once kopia is already reading.
    """
    _install_stubs(monkeypatch)
    boom = CommandError(CommandResult(["nbdcopy"], 5, "", "broken pipe"))
    snapper = FakeSnapper(disks=[_disk_target()], stream_error=boom)
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    # ``commit`` ran exactly once despite the streamer raising. The capture
    # also pins the structured-error log so operators see the disk-level fault.
    assert len(snapper.commit_calls) == 1
    assert "disk snapshot failed" in capsys.readouterr().err


def test_freeze_failure_returns_failed_vm_result_without_cleanup(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = _install_stubs(monkeypatch)

    class FreezeFailSnapper(FakeSnapper):
        def freeze(self, vm_name: str, disks: list[DiskTarget]) -> FrozenSnapshot:
            self.freeze_calls.append((vm_name, list(disks)))
            raise CommandError(CommandResult(["virsh"], 1, "", "snapshot exploded"))

    snapper = FreezeFailSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    record = _find_event(capsys.readouterr().err, "snapshot freeze failed")
    assert record["vm"] == "alpha"
    assert record["stderr"] == "snapshot exploded"
    assert snapper.stream_calls == []
    assert snapper.commit_calls == []
    assert captured["create_stdin"] == []


def test_backup_vm_rejects_non_file_disk_before_manifest(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = _install_stubs(monkeypatch)
    snapper = FakeSnapper(disks=[_disk_target(source="/dev/vg/alpha", source_type="block")])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "unsupported backup disk" in capsys.readouterr().err
    assert captured["domain_xml"] == []
    assert snapper.freeze_calls == []


def test_backup_vm_rejects_non_qcow2_format_before_manifest(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = _install_stubs(monkeypatch, disk_format="raw")
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "only qcow2 disks are supported" in capsys.readouterr().err
    assert captured["domain_xml"] == []
    assert snapper.freeze_calls == []


def test_missing_snapshot_base_logs_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)
    manifest = manifest_module.Manifest(
        vm_name="alpha",
        vm_uuid=ALPHA_UUID,
        vm_state="running",
        host_id="host-a",
        run_id="run-1",
        timestamp="20260101T010101",
        libvirt_uri="qemu:///system",
        domain_xml="<domain/>",
        disks=(
            manifest_module.ManifestDisk(
                target="vda",
                source_path="/img/vda.qcow2",
                virtual_size_bytes=1,
                snapshot_filename="vda.raw",
            ),
        ),
    )
    snapper = FakeSnapper(disks=[_disk_target("vdb", "/img/vdb.qcow2")])
    ok, _consistency = backup._stream_all_disks(
        backup_config, _vm(), manifest, "run-1", snapper, [_disk_target("vdb", "/img/vdb.qcow2")]
    )
    assert ok is False
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


def test_build_manifest_records_vm_state(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    _install_stubs(monkeypatch)
    manifest = backup._build_manifest(
        backup_config, _vm(state="shut off"), "run-1", "20260101T010101", [_disk_target()]
    )
    assert manifest is not None
    assert manifest.vm_state == "shut off"


def test_override_source_format(backup_config: Config) -> None:
    backup_config.values["HOST_ID"] = "host-a"
    assert backup._override_source(backup_config, ALPHA_UUID, "meta") == f"root@host-a:libvirt-backup:{ALPHA_UUID}/meta"


def test_disk_and_meta_tags(backup_config: Config) -> None:
    backup_config.values["HOST_ID"] = "host-a"
    vm = _vm(uuid=BETA_UUID)
    disk_tags = backup._disk_tags(backup_config, vm, "run-9", "vda")
    assert disk_tags == {"vm-uuid": BETA_UUID, "disk": "vda", "host": "host-a", "run-id": "run-9", "kind": "disk"}
    meta_tags = backup._meta_tags(backup_config, vm, "run-9", "20260101T010101", "filesystem")
    assert meta_tags == {
        "vm-uuid": BETA_UUID,
        "vm-name": vm.name,
        "host": "host-a",
        "run-id": "run-9",
        "kind": "meta",
        "timestamp": "20260101T010101",
        "consistency": "filesystem",
    }


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
