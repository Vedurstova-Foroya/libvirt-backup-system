"""Unit tests for the kopia-engine ``backup`` orchestrator.

The orchestrator is deliberately wired through module-level names so unit
tests can stub the kopia and libvirt boundaries with ``monkeypatch.setattr``
and exercise the happy + sad paths without touching ``virsh``, ``qemu-nbd``,
or a real kopia repo. ``run_backups`` lives in ``test_backup_run.py`` so
each file stays under the project's 300-LOC ceiling.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import backup, kopia_snapshots
from libvirt_backup_system.config import Config
from libvirt_backup_system.disks import DiskEntry
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vm_snapshot import DiskTarget, FrozenSnapshot
from libvirt_backup_system.vms import VM

from .conftest import ALPHA_UUID


@dataclass
class _FakeUpstream:
    """Stand-in for the ``Popen`` returned by ``stream_disk``.

    ``snapshot_create_stdin`` is monkeypatched, so we never need to wire a
    real ``stdout`` pipe; the attribute exists so ``backup`` can pass the
    object straight through without raising.
    """

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
    ) -> None:
        self.disks = disks
        self.quiesced = quiesced
        self.commit_error = commit_error
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
            quiesced=self.quiesced,
        )

    @contextmanager
    def stream_disk(self, base: Path) -> Iterator[_FakeUpstream]:
        self.stream_calls.append(base)
        yield _FakeUpstream(args=["qemu-nbd", str(base)])

    def commit(self, snapshot: FrozenSnapshot) -> None:
        self.commit_calls.append(snapshot)
        if self.commit_error is not None:
            raise self.commit_error


def _vm(name: str = "alpha", uuid: str = ALPHA_UUID, state: str = "running") -> VM:
    return VM(name=name, state=state, uuid=uuid)


def _disk_entry(target: str = "vda", source: str = "/img/alpha.qcow2") -> DiskEntry:
    return DiskEntry(target=target, source=Path(source))


def _disk_target(target: str = "vda", source: str = "/img/alpha.qcow2") -> DiskTarget:
    return DiskTarget(target=target, source=Path(source))


def _install_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    disks: list[DiskEntry] | None = None,
    virtual_size: object = 1024,
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
        "virtual_size": [],
        "domain_xml": [],
        "mount_checks": [],
    }
    disks = disks if disks is not None else [_disk_entry()]

    def fake_vm_disks(uri: str, name: str) -> list[DiskEntry]:
        return list(disks)

    def fake_virtual_size(path: str) -> int:
        captured["virtual_size"].append(path)
        if isinstance(virtual_size, BaseException):
            raise virtual_size
        return int(virtual_size)

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
            create_stdin(**kwargs)

    def default_create_path(**kwargs: Any) -> None:
        captured["create_path"].append(kwargs)
        if create_path is not None:
            create_path(**kwargs)

    monkeypatch.setattr(backup, "vm_disk_paths_with_targets", fake_vm_disks)
    monkeypatch.setattr(backup, "disk_virtual_size_bytes", fake_virtual_size)
    monkeypatch.setattr(backup, "_read_domain_xml", fake_read_xml)
    monkeypatch.setattr(backup, "runtime_backup_path_ok", fake_mount)
    monkeypatch.setattr(kopia_snapshots, "snapshot_create_stdin", default_create_stdin)
    monkeypatch.setattr(kopia_snapshots, "snapshot_create_path", default_create_path)
    # The default ``LibvirtSnapshotter`` constructed inside ``_stream_single_disk``
    # would shell out to virsh/qemu-nbd. Substitute a no-op stand-in whose
    # ``stream_disk`` simply yields a fake upstream so the kopia stub sees the
    # exact object backup.py would have passed in.
    monkeypatch.setattr(backup, "LibvirtSnapshotter", _NoopLibvirtSnapshotter)
    return captured


class _NoopLibvirtSnapshotter:
    """Replacement for ``LibvirtSnapshotter`` used in ``_stream_single_disk``.

    The real implementation spawns ``qemu-nbd`` + ``nbdcopy``; here we only
    need ``stream_disk`` to provide an iterable context manager that yields
    something the patched ``snapshot_create_stdin`` accepts.
    """

    def __init__(self, *, libvirt_uri: str) -> None:
        self.libvirt_uri = libvirt_uri

    @contextmanager
    def stream_disk(self, base: Path) -> Iterator[_FakeUpstream]:
        yield _FakeUpstream(args=["nbdcopy", str(base)])


def test_current_month_uses_provided_datetime() -> None:
    assert backup.current_month(dt.datetime(2024, 3, 5, tzinfo=dt.timezone.utc)) == "2024-03"


def test_current_month_defaults_to_now() -> None:
    out = backup.current_month()
    assert len(out) == 7 and out[4] == "-"


def test_timestamp_wraps_utc_timestamp() -> None:
    now = dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    assert backup.timestamp(now) == "20240102T030405"


def test_backup_vm_happy_path_quiesces_and_streams_each_disk(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    disk_entries = [_disk_entry("vda", "/img/vda.qcow2"), _disk_entry("vdb", "/img/vdb.qcow2")]
    captured = _install_stubs(monkeypatch, disks=disk_entries, virtual_size=4096)
    snapper = FakeSnapper(disks=[_disk_target("vda", "/img/vda.qcow2"), _disk_target("vdb", "/img/vdb.qcow2")])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
    assert snapper.freeze_calls and snapper.commit_calls
    # ``_stream_single_disk`` builds a *fresh* ``LibvirtSnapshotter`` for
    # the actual streaming (patched here to the no-op stand-in); the snapper
    # passed to ``backup_vm`` only owns ``list_disks`` / ``freeze`` / ``commit``.
    assert snapper.list_disks_calls == ["alpha"]
    stdin_targets = sorted(call["stdin_file"] for call in captured["create_stdin"])
    assert stdin_targets == ["vda.raw", "vdb.raw"]
    # Two disk snapshots + one meta snapshot; the meta carries the kind tag.
    assert len(captured["create_path"]) == 1
    assert captured["create_path"][0]["tags"]["kind"] == "meta"
    # Mount check fires at preflight and again post-meta.
    assert len(captured["mount_checks"]) == 2


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


def test_backup_vm_uses_default_snapper_when_none(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    _install_stubs(monkeypatch)
    instantiated: list[str] = []

    class FakeDefault(FakeSnapper):
        def __init__(self, *, libvirt_uri: str) -> None:
            instantiated.append(libvirt_uri)
            super().__init__(disks=[_disk_target()])

    # backup.LibvirtSnapshotter is already replaced with the no-op; we only
    # need a Snapshotter-shaped class for the ``snapper or LibvirtSnapshotter``
    # fallback to construct on its own.
    monkeypatch.setattr(backup, "LibvirtSnapshotter", FakeDefault)
    assert backup.backup_vm(backup_config, _vm()) is True
    assert instantiated  # at least the orchestrator's fallback used the default


def test_backup_vm_logs_when_virtual_size_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    exc = CommandError(CommandResult(["qemu-img"], 1, "", "boom"))
    captured = _install_stubs(monkeypatch, virtual_size=exc)
    snapper = FakeSnapper(disks=[_disk_target()])
    assert backup.backup_vm(backup_config, _vm(), snapper=snapper) is True
    assert "could not read virtual disk size" in capsys.readouterr().err
    # Disk snapshot still ran despite the size lookup blowing up.
    assert len(captured["create_stdin"]) == 1
