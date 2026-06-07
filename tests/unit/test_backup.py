"""Unit tests for the kopia-engine ``backup`` orchestrator.

The orchestrator is deliberately wired through module-level names so unit
tests can stub the kopia and libvirt boundaries with ``monkeypatch.setattr``
and exercise the happy + sad paths without touching ``virsh``, ``qemu-nbd``,
or a real kopia repo. ``run_backups`` lives in ``test_backup_run.py`` so
each file stays under the project's 300-LOC ceiling.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import backup, kopia_snapshots
from libvirt_backup_system.config import Config
from libvirt_backup_system.consistency import CRASH, FILESYSTEM
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vm_snapshot import DiskTarget, FrozenSnapshot
from libvirt_backup_system.vms import VM

from .conftest import ALPHA_UUID


def _find_event(captured: str, message: str) -> dict[str, Any]:
    """Locate a structured ``event`` record by message in captured stdout/stderr.

    ``logging_json.event`` writes one JSON document per line; tests assert
    on the parsed dict rather than substring-matching the message and the
    field side-by-side. Raises ``AssertionError`` so test failures point at
    the missing event rather than a ``KeyError`` later in the call chain.
    """
    for line in captured.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("message") == message:
            return dict(record)
    raise AssertionError(f"no event with message {message!r} in: {captured!r}")


@dataclass
class _FakeUpstream:
    args: list[str]
    returncode: int = 0
    stdout: object = None


class FakeSnapper:
    """In-memory ``VmSnapshotter`` mirror used by the backup tests.

    The real ``LibvirtSnapshotter`` shells out for every method; the unit
    tests only need to verify that backup.py calls the protocol methods in
    the right order with the right arguments.
    """

    def __init__(
        self,
        disks: list[DiskTarget],
        *,
        quiesced: bool = True,
        commit_error: CommandError | None = None,
        stream_error: BaseException | None = None,
    ) -> None:
        self.disks = disks
        self.quiesced = quiesced
        self.commit_error = commit_error
        self.stream_error = stream_error
        self.list_disks_calls: list[str] = []
        self.freeze_calls: list[tuple[str, list[DiskTarget]]] = []
        self.stream_calls: list[Path] = []
        self.commit_calls: list[FrozenSnapshot] = []

    def list_disks(self, vm_name: str) -> list[DiskTarget]:
        self.list_disks_calls.append(vm_name)
        return list(self.disks)

    def freeze(self, vm_name: str, disks: list[DiskTarget]) -> FrozenSnapshot:
        self.freeze_calls.append((vm_name, list(disks)))
        return FrozenSnapshot(
            vm_name=vm_name,
            snapshot_name="snap-1",
            overlays={d.target: Path(f"/tmp/{d.target}.overlay") for d in disks},
            bases=tuple(disks),
            consistency=FILESYSTEM if self.quiesced else CRASH,
        )

    @contextmanager
    def stream_disk(self, base: Path) -> Iterator[_FakeUpstream]:
        self.stream_calls.append(base)
        if self.stream_error is not None:
            # Raise *after* yielding so the caller's ``with`` block enters
            # before the failure, mirroring how a ``qemu-nbd``/``nbdcopy``
            # pipeline dies mid-stream. The orchestrator must still run
            # ``commit`` to fold the overlay back into the live disk.
            yield _FakeUpstream(args=["qemu-nbd", str(base)])
            raise self.stream_error
        yield _FakeUpstream(args=["qemu-nbd", str(base)])

    def commit(self, snapshot: FrozenSnapshot) -> None:
        self.commit_calls.append(snapshot)
        if self.commit_error is not None:
            raise self.commit_error


def _vm(name: str = "alpha", uuid: str = ALPHA_UUID, state: str = "running") -> VM:
    return VM(name=name, state=state, uuid=uuid)


def _disk_target(target: str = "vda", source: str = "/img/alpha.qcow2", source_type: str = "file") -> DiskTarget:
    return DiskTarget(target=target, source=Path(source), source_type=source_type)


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    virtual_size: object = 1024,
    disk_format: str = "qcow2",
    domain_xml: str = "<domain/>",
    mount_ok: object = True,
    create_stdin: Any = None,
    create_path: Any = None,
) -> dict[str, list[Any]]:
    """Wire backup.py's module-level dependencies to in-memory fakes.

    Returns a dict of call-capture lists so tests can assert on the args
    flowing through each stub. ``mount_ok`` may be a bool (uniform answer)
    or a list (popped per call) so the post-write mount check can flip
    independently of the preflight check.
    """
    captured: dict[str, list[Any]] = {
        "create_stdin": [],
        "create_path": [],
        "delete": [],
        "disk_info": [],
        "domain_xml": [],
        "mount_checks": [],
    }

    def fake_disk_info(path: str) -> dict[str, object]:
        captured["disk_info"].append(path)
        if isinstance(virtual_size, BaseException):
            raise virtual_size
        return {"format": disk_format, "virtual-size": int(virtual_size)}

    def fake_read_xml(uri: str, name: str) -> str:
        captured["domain_xml"].append((uri, name))
        return domain_xml

    def fake_mount(config: Config) -> bool:
        answer = (mount_ok.pop(0) if mount_ok else True) if isinstance(mount_ok, list) else mount_ok
        captured["mount_checks"].append(answer)
        return bool(answer)

    def default_create_stdin(**kwargs: Any) -> None:
        captured["create_stdin"].append(kwargs)
        if create_stdin is not None:
            result = create_stdin(**kwargs)
            if isinstance(result, str):
                return result
        return f"snap-{kwargs['stdin_file']}"

    def default_create_path(**kwargs: Any) -> None:
        captured_call = dict(kwargs)
        manifest_path = Path(kwargs["path"]) / "manifest.json"
        if manifest_path.exists():
            captured_call["manifest_json"] = manifest_path.read_text(encoding="utf-8")
        captured["create_path"].append(captured_call)
        if create_path is not None:
            create_path(**kwargs)

    def default_delete(**kwargs: Any) -> None:
        captured["delete"].append(kwargs)

    monkeypatch.setattr(backup, "disk_image_info", fake_disk_info)
    monkeypatch.setattr(backup, "_read_domain_xml", fake_read_xml)
    monkeypatch.setattr(backup, "runtime_backup_path_ok", fake_mount)
    monkeypatch.setattr(kopia_snapshots, "snapshot_create_stdin", default_create_stdin)
    monkeypatch.setattr(kopia_snapshots, "snapshot_create_path", default_create_path)
    monkeypatch.setattr(kopia_snapshots, "snapshot_delete", default_delete)
    return captured


def test_timestamp_wraps_utc_timestamp() -> None:
    now = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    assert backup.timestamp(now) == "20240102T030405"


def test_backup_vm_happy_path_quiesces_and_streams_each_disk(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = _install_stubs(monkeypatch, virtual_size=4096)
    snapper = FakeSnapper(disks=[_disk_target("vda", "/img/vda.qcow2"), _disk_target("vdb", "/img/vdb.qcow2")])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
    assert snapper.freeze_calls and snapper.commit_calls
    # The injected snapper is the *same* object every disk streams through;
    # backup.py no longer constructs a fresh ``LibvirtSnapshotter`` inside
    # ``_stream_single_disk``. The fake records each stream call so we can
    # pin the architectural contract.
    assert snapper.list_disks_calls == ["alpha"]
    assert sorted(p.name for p in snapper.stream_calls) == ["vda.qcow2", "vdb.qcow2"]
    stdin_targets = sorted(call["stdin_file"] for call in captured["create_stdin"])
    assert stdin_targets == ["vda.raw", "vdb.raw"]
    # Two disk snapshots + one meta snapshot; the meta carries consistency.
    assert len(captured["create_path"]) == 1
    assert captured["create_path"][0]["tags"]["kind"] == "meta"
    assert captured["create_path"][0]["tags"]["consistency"] == "filesystem"
    assert json.loads(captured["create_path"][0]["manifest_json"])["consistency"] == "filesystem"
    # Mount check fires at preflight and again post-meta.
    assert len(captured["mount_checks"]) == 2
    # The completion event must surface the quiesce outcome so operators can
    # grep run logs for VMs that fell back to crash-consistent snapshots.
    completion = _find_event(capsys.readouterr().out, "backup completed")
    assert completion["consistency"] == "filesystem"


def test_backup_vm_rejects_unsafe_name(backup_config: Config) -> None:
    with pytest.raises(ValueError, match="unsafe VM name"):
        backup.backup_vm(backup_config, VM(name="../bad", state="running", uuid=ALPHA_UUID))


def test_backup_vm_rejects_unsafe_uuid(backup_config: Config) -> None:
    with pytest.raises(ValueError, match="unsafe VM uuid"):
        backup.backup_vm(backup_config, VM(name="alpha", state="running", uuid="not-a-uuid"))


def test_backup_vm_bails_when_mount_unavailable_at_start(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    _install_stubs(monkeypatch, mount_ok=False)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    # Mount probe failed before any libvirt work happened.
    assert snapper.freeze_calls == []


def test_backup_vm_post_meta_mount_loss_logs_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # First call (preflight) succeeds, second call (after meta) fails.
    _install_stubs(monkeypatch, mount_ok=[True, False])
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "backup completed but backup path no longer mounted" in capsys.readouterr().err


def test_backup_vm_commits_when_stream_spawn_raises_oserror(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    _install_stubs(monkeypatch)

    class SpawnFailSnapper(FakeSnapper):
        @contextmanager
        def stream_disk(self, base: Path) -> Iterator[_FakeUpstream]:
            self.stream_calls.append(base)
            raise OSError("qemu-nbd missing")
            yield _FakeUpstream(args=["qemu-nbd", str(base)])

    snapper = SpawnFailSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert snapper.commit_calls
    assert "disk snapshot failed" in capsys.readouterr().err


def test_backup_vm_uses_default_snapper_when_none(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    _install_stubs(monkeypatch)
    instantiated: list[tuple[str, object]] = []

    class FakeDefault(FakeSnapper):
        def __init__(self, libvirt_uri: str, **kwargs: object) -> None:
            instantiated.append((libvirt_uri, kwargs["socket_root"]))
            super().__init__(disks=[_disk_target()])

    # When no snapper is injected, ``backup_vm`` constructs one from
    # ``backup.LibvirtSnapshotter``; the patched ``FakeDefault`` exercises
    # that fallback while keeping the streaming path off virsh/qemu-nbd.
    monkeypatch.setattr(backup, "LibvirtSnapshotter", FakeDefault)
    assert backup.backup_vm(backup_config, _vm()) is True
    assert instantiated == [(backup_config.get("LIBVIRT_URI"), backup_config.prefix / "var/lib/libvirt/qemu")]


def test_backup_vm_rejects_disk_info_failure_before_manifest(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exc = CommandError(CommandResult(["qemu-img"], 1, "", "boom"))
    captured = _install_stubs(monkeypatch, virtual_size=exc)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is False
    assert "unsupported backup disk" in capsys.readouterr().err
    assert captured["domain_xml"] == []
    assert snapper.freeze_calls == []
