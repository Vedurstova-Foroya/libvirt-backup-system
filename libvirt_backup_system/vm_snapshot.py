"""Hypervisor-agnostic snapshot/stream/commit interface.

Phase 2 of the kopia migration. The libvirt implementation in this file
drives ``virsh snapshot-create-as`` (external, disk-only, atomic, with QGA
quiesce when possible) so the base qcow2 freezes read-only for the duration
of the backup, opens a ``qemu-nbd`` server on the base, hands the resulting
stream to a consumer via ``nbdcopy``, then folds the overlay back in with
``virsh blockcommit --active --pivot --shallow``.

A future ``vm_snapshot_hyperv`` module slots in behind the same
``VmSnapshotter`` protocol; nothing here reaches into kopia, so the
hypervisor shim stays pure.
"""

from __future__ import annotations

import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .logging_json import event
from .shell import CommandError, CommandResult, run

# Per-domain runtime root libvirt creates for each running VM.
LIBVIRT_QEMU_RUNTIME_ROOT = Path("/var/lib/libvirt/qemu")


@dataclass(frozen=True)
class DiskTarget:
    """One (target-dev, base-qcow2) pair extracted from libvirt domblklist."""

    target: str
    source: Path


@dataclass(frozen=True)
class FrozenSnapshot:
    """Result of ``freeze``: external snapshots written, base files read-only."""

    vm_name: str
    snapshot_name: str
    overlays: dict[str, Path]  # target -> overlay file produced by libvirt
    bases: tuple[DiskTarget, ...]
    quiesced: bool


class VmSnapshotter(Protocol):  # pragma: no cover - type-checker fiction at runtime
    """Hypervisor-agnostic snapshot/stream/commit interface.

    ``stream_disk`` returns a context manager whose yielded value is opaque
    (``object``) — the orchestrator only forwards it straight into the
    storage backend, so a future ``vm_snapshot_hyperv`` is free to yield a
    Hyper-V VHDX reader, a Windows handle, or whatever — as long as the
    storage layer (kopia) can consume it. Pinning the exact wrapper class
    would force every future provider into the same Popen-shaped contract.
    """

    def list_disks(self, vm_name: str) -> list[DiskTarget]: ...
    def freeze(self, vm_name: str, disks: list[DiskTarget]) -> FrozenSnapshot: ...
    def stream_disk(self, base: Path) -> AbstractContextManager[object]: ...
    def commit(self, snapshot: FrozenSnapshot) -> None: ...


@dataclass
class LibvirtSnapshotter:
    libvirt_uri: str
    qemu_nbd_path: str = "qemu-nbd"
    nbdcopy_path: str = "nbdcopy"
    socket_root: Path = LIBVIRT_QEMU_RUNTIME_ROOT

    def list_disks(self, vm_name: str) -> list[DiskTarget]:
        """Return ``(target, source)`` pairs via ``virsh domblklist --details``.

        ``--details`` adds the ``Type Device Target Source`` header; we filter
        to ``device == 'disk'`` entries so CD-ROMs and floppies do not get
        backed up. Pure-network disks (no source path) are skipped — the
        kopia chunker cannot reach them from this host.
        """
        result = run(["virsh", "-c", self.libvirt_uri, "domblklist", "--details", "--", vm_name])
        disks: list[DiskTarget] = []
        for raw_line in result.stdout.splitlines():
            parts = raw_line.split(None, 3)
            if len(parts) != 4 or parts[0] == "Type":
                continue
            _type, device, target, source = parts
            if device != "disk" or not source or source == "-":
                continue
            disks.append(DiskTarget(target=target, source=Path(source)))
        return disks

    def freeze(self, vm_name: str, disks: list[DiskTarget]) -> FrozenSnapshot:
        """Create an external snapshot, attempting QGA quiesce.

        Each disk gets its own overlay file in the libvirt per-VM runtime
        directory so the new file inherits the dynamic libvirt-<uuid>
        AppArmor profile. Quiesce is best-effort: a QGA failure retries
        without it and logs a warning rather than aborting the run.
        """
        snapshot_name = f"lbs-{secrets.token_hex(6)}-{int(time.time())}"
        runtime = self.socket_root / f"domain-libvirt-backup-{vm_name}"
        diskspecs: list[str] = []
        overlays: dict[str, Path] = {}
        for disk in disks:
            overlay = runtime / f"{disk.target}.{snapshot_name}.overlay"
            overlays[disk.target] = overlay
            diskspecs.extend(["--diskspec", f"{disk.target},snapshot=external,file={overlay}"])
        quiesced = self._snapshot_create(vm_name, snapshot_name, diskspecs)
        return FrozenSnapshot(
            vm_name=vm_name,
            snapshot_name=snapshot_name,
            overlays=overlays,
            bases=tuple(disks),
            quiesced=quiesced,
        )

    def _snapshot_create(self, vm_name: str, snapshot_name: str, diskspecs: list[str]) -> bool:
        base_args = [
            "virsh",
            "-c",
            self.libvirt_uri,
            "snapshot-create-as",
            "--domain",
            vm_name,
            "--name",
            snapshot_name,
            "--disk-only",
            "--atomic",
            "--no-metadata",
            *diskspecs,
        ]
        try:
            run([*base_args, "--quiesce"])
            return True
        except CommandError as exc:
            event(
                "warning",
                "QGA quiesce failed; retrying without quiesce (crash-consistent)",
                vm=vm_name,
                stderr=exc.result.stderr.strip(),
            )
        run(base_args)
        return False

    @contextmanager
    def stream_disk(self, base: Path) -> Iterator[subprocess.Popen[bytes]]:
        """Open the base qcow2 read-only and yield an ``nbdcopy`` stream.

        Lifecycle:
          1. spawn ``qemu-nbd -r --persistent --shared=4 --socket=<sock>``
          2. spawn ``nbdcopy nbd+unix://<sock> -`` and yield its process
          3. on exit, terminate both processes and unlink the socket
        The caller pipes ``proc.stdout`` directly into ``kopia snapshot
        create --stdin-file=...``; the consumer (kopia) ``communicate()``s
        with this process to drain the bytes.
        """
        socket = self.socket_root / f"vnbd-{secrets.token_hex(8)}.sock"
        qemu_nbd: subprocess.Popen[bytes] | None = None
        nbdcopy: subprocess.Popen[bytes] | None = None
        try:
            qemu_nbd = subprocess.Popen(
                [
                    self.qemu_nbd_path,
                    "-r",
                    "--persistent",
                    "--shared=4",
                    f"--socket={socket}",
                    str(base),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._await_socket(socket, qemu_nbd)
            nbdcopy = subprocess.Popen(
                [self.nbdcopy_path, f"nbd+unix://?socket={socket}", "-"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            yield nbdcopy
        finally:
            self._terminate(nbdcopy)
            self._terminate(qemu_nbd)
            with suppress(OSError):
                socket.unlink(missing_ok=True)

    def _await_socket(self, socket: Path, proc: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if socket.exists():
                return
            if proc.poll() is not None:
                raise CommandError(
                    self._command_result(proc, [self.qemu_nbd_path], "qemu-nbd exited before socket appeared")
                )
            time.sleep(0.05)
        raise CommandError(self._command_result(proc, [self.qemu_nbd_path], "qemu-nbd socket did not appear"))

    @staticmethod
    def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None:
            return
        if proc.poll() is None:
            with suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(OSError):
                    proc.kill()
                with suppress(subprocess.TimeoutExpired, OSError):
                    proc.wait(timeout=5)

    @staticmethod
    def _command_result(proc: subprocess.Popen[bytes], args: list[str], msg: str) -> CommandResult:
        stderr = ""
        if proc.stderr is not None:
            with suppress(OSError):
                stderr = proc.stderr.read().decode("utf-8", errors="replace")
        return CommandResult(args=args, returncode=proc.returncode or 1, stdout="", stderr=f"{msg}: {stderr}")

    def commit(self, snapshot: FrozenSnapshot) -> None:
        """Fold each overlay back into its base via ``blockcommit --pivot``.

        Errors are logged and re-raised; the orchestrator may want to leave
        a wedged overlay in place rather than push half-committed state.
        After every overlay has been unlinked, attempt to remove the per-VM
        runtime staging dir that ``freeze`` created. ``rmdir`` only succeeds
        on an empty directory, so any leftover state (a failed commit's
        wedged overlay, libvirt-owned files, etc.) is left untouched.
        """
        failures: list[CommandError] = []
        runtime_dirs: set[Path] = set()
        for disk in snapshot.bases:
            try:
                run(
                    [
                        "virsh",
                        "-c",
                        self.libvirt_uri,
                        "blockcommit",
                        snapshot.vm_name,
                        disk.target,
                        "--active",
                        "--pivot",
                        "--shallow",
                    ]
                )
            except CommandError as exc:
                event(
                    "error",
                    "blockcommit failed; overlay left in place",
                    vm=snapshot.vm_name,
                    target=disk.target,
                    stderr=exc.result.stderr.strip(),
                )
                failures.append(exc)
                continue
            overlay = snapshot.overlays.get(disk.target)
            if overlay is not None:
                runtime_dirs.add(overlay.parent)
                with suppress(FileNotFoundError, OSError):
                    overlay.unlink()
        for runtime in runtime_dirs:
            with suppress(OSError):
                runtime.rmdir()
        if failures:
            raise failures[0]
