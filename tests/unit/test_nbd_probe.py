from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.nbd_probe import (
    FALLBACK_PROBE_SOCKET_NAME,
    domain_socket_path,
    probe_qemu_socket_bind,
    virtnbdbackup_socket_args,
)
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


class _Recorder:
    def __init__(self, *, start: CommandResult | Exception, stop: CommandResult | None = None) -> None:
        self.start = start
        self.stop = stop or CommandResult(args=[], returncode=0, stdout="", stderr="")
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], *, check: bool = True, env: object = None) -> CommandResult:  # noqa: ARG002
        self.calls.append(args)
        # First call is nbd_server_start (we stub domain_socket_path so virsh
        # domid is not invoked by the probe path), subsequent calls are stop.
        if len(self.calls) == 1:
            if isinstance(self.start, Exception):
                raise self.start
            return self.start
        return self.stop


def _running_vms() -> list[VM]:
    return [VM("alpha", "running", ALPHA_UUID), VM("beta", "shut off", BETA_UUID)]


def _ok() -> CommandResult:
    return CommandResult(args=[], returncode=0, stdout="", stderr="")


def _patch_fallback_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.FALLBACK_SCRATCH_DIR", tmp_path)
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.domain_socket_path", lambda uri, name, **_: None)
    return tmp_path / FALLBACK_PROBE_SOCKET_NAME


def test_probe_skips_when_no_running_vms(backup_config: Config, monkeypatch) -> None:
    recorder = _Recorder(start=_ok())
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)
    assert probe_qemu_socket_bind(backup_config, [VM("alpha", "shut off", ALPHA_UUID)]) == []
    assert recorder.calls == []


def test_probe_skips_for_remote_uri(backup_config: Config, monkeypatch) -> None:
    backup_config.values["LIBVIRT_URI"] = "qemu+ssh://kvm-host/system"
    recorder = _Recorder(start=_ok())
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)
    assert probe_qemu_socket_bind(backup_config, _running_vms()) == []
    assert recorder.calls == []


def test_probe_skips_for_test_uri(backup_config: Config, monkeypatch) -> None:
    backup_config.values["LIBVIRT_URI"] = "test:///default"
    recorder = _Recorder(start=_ok())
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)
    assert probe_qemu_socket_bind(backup_config, _running_vms()) == []
    assert recorder.calls == []


def test_probe_success_calls_stop_and_removes_socket(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    socket_path = _patch_fallback_dir(monkeypatch, tmp_path)
    socket_path.write_bytes(b"stale")
    recorder = _Recorder(start=_ok())
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)

    assert probe_qemu_socket_bind(backup_config, _running_vms()) == []
    assert len(recorder.calls) == 2
    start_cmd, stop_cmd = recorder.calls
    assert start_cmd[-2:] == ["alpha", f"nbd_server_start unix:{socket_path}"]
    assert stop_cmd[-2:] == ["alpha", "nbd_server_stop"]
    assert not socket_path.exists()


def test_probe_uses_domain_path_when_available(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    chosen = tmp_path / "domain-2-alpha" / "vnbd-preflight.sock"
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.domain_socket_path", lambda uri, name, **_: chosen)
    recorder = _Recorder(start=_ok())
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)

    assert probe_qemu_socket_bind(backup_config, _running_vms()) == []
    assert recorder.calls[0][-1] == f"nbd_server_start unix:{chosen}"


def test_probe_detects_permission_denied(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    socket_path = _patch_fallback_dir(monkeypatch, tmp_path)
    denied = CommandResult(args=[], returncode=1, stdout="", stderr="error: bind: Permission denied")
    recorder = _Recorder(start=denied)
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)

    failures = probe_qemu_socket_bind(backup_config, _running_vms())
    assert len(failures) == 1
    assert "QEMU cannot bind NBD socket" in failures[0]
    assert str(socket_path) in failures[0]
    assert "AppArmor" in failures[0] or "SELinux" in failures[0]
    # nbd_server_stop must NOT run when our start failed: a concurrent backup
    # may own the NBD slot and our stop would tear it down.
    assert not any("nbd_server_stop" in arg for call in recorder.calls for arg in call)


def test_probe_reports_other_virsh_failure(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    _patch_fallback_dir(monkeypatch, tmp_path)
    error = CommandResult(args=[], returncode=1, stdout="", stderr="error: Domain not found: alpha")
    recorder = _Recorder(start=error)
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)

    failures = probe_qemu_socket_bind(backup_config, _running_vms())
    assert len(failures) == 1
    assert "QEMU NBD socket bind probe failed" in failures[0]
    assert "Domain not found" in failures[0]


def test_probe_reports_os_error_invoking_virsh(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    _patch_fallback_dir(monkeypatch, tmp_path)
    recorder = _Recorder(start=OSError("virsh missing"))
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", recorder)

    failures = probe_qemu_socket_bind(backup_config, _running_vms())
    assert len(failures) == 1
    assert "failed to invoke virsh" in failures[0]
    assert len(recorder.calls) == 1


def test_probe_reports_stale_socket_unlink_failure(backup_config: Config, monkeypatch, tmp_path: Path) -> None:
    socket_path = _patch_fallback_dir(monkeypatch, tmp_path)
    original_unlink = Path.unlink

    def refuse_probe_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self == socket_path:
            raise OSError("stale socket is wedged")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.Path.unlink", refuse_probe_unlink)
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", lambda *a, **kw: pytest.fail("must not invoke virsh"))

    failures = probe_qemu_socket_bind(backup_config, _running_vms())
    assert len(failures) == 1
    assert "could not clean stale socket" in failures[0]


def test_domain_socket_path_returns_none_for_remote_uri(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", lambda *a, **kw: pytest.fail("must not invoke virsh"))
    assert domain_socket_path("qemu+ssh://host/system", "alpha") is None


def test_domain_socket_path_returns_none_when_virsh_oserror(monkeypatch) -> None:
    def fail(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise OSError("no virsh")

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", fail)
    assert domain_socket_path("qemu:///system", "alpha") is None


@pytest.mark.parametrize(
    ("rc", "stdout"),
    [(1, ""), (0, "-"), (0, ""), (0, "not-a-number"), (0, "1.5")],
)
def test_domain_socket_path_returns_none_for_invalid_domid(monkeypatch, rc: int, stdout: str) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, rc, stdout, ""),
    )
    assert domain_socket_path("qemu:///system", "alpha") is None


def test_domain_socket_path_returns_none_when_dir_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, 0, "2\n", ""),
    )
    assert domain_socket_path("qemu:///system", "alpha") is None


def test_domain_socket_path_returns_none_when_parent_stat_denied(monkeypatch, tmp_path: Path) -> None:
    # is_dir raises PermissionError when running unprivileged against the
    # libvirt-qemu-owned root; treat that the same as missing rather than
    # propagating as a fatal in preflight (the e2e session-mode scenario).
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", tmp_path)
    (tmp_path / "domain-2-alpha").mkdir()

    def deny(self: Path) -> bool:
        raise PermissionError("stat denied")

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.Path.is_dir", deny)
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, 0, "2\n", ""),
    )
    assert domain_socket_path("qemu:///system", "alpha") is None


def test_domain_socket_path_returns_path_when_dir_exists(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", tmp_path)
    (tmp_path / "domain-2-alpha").mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, 0, "2\n", ""),
    )
    result = domain_socket_path("qemu:///system", "alpha")
    assert result == tmp_path / "domain-2-alpha" / "vnbd.sock"


def test_domain_socket_path_treats_qemu_unix_system_as_local(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", tmp_path)
    (tmp_path / "domain-2-alpha").mkdir()
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return CommandResult(args, 0, "2\n", "")

    monkeypatch.setattr("libvirt_backup_system.nbd_probe.run", fake_run)
    result = domain_socket_path("qemu+unix:///system", "alpha")
    assert result == tmp_path / "domain-2-alpha" / "vnbd.sock"
    assert calls[0][2] == "qemu+unix:///system"


def test_domain_socket_path_truncates_long_vm_name(monkeypatch, tmp_path: Path) -> None:
    # Mirrors libvirt's VIR_DOMAIN_SHORT_NAME_MAX=20 so the resolved path lines
    # up with the directory libvirt actually creates and the AppArmor rule the
    # dynamic per-VM profile grants. The first 20 chars of the name are kept.
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", tmp_path)
    long_name = "a" * 35
    short = "a" * 20
    (tmp_path / f"domain-2-{short}").mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, 0, "2\n", ""),
    )
    result = domain_socket_path("qemu:///system", long_name)
    assert result == tmp_path / f"domain-2-{short}" / "vnbd.sock"


def test_domain_socket_path_returns_none_when_path_too_long(monkeypatch) -> None:
    # Force a deep root so even the 20-char-truncated name pushes past 107B.
    deep = Path("/" + "x" * 150)
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.LIBVIRT_QEMU_RUNTIME_ROOT", deep)
    monkeypatch.setattr(
        "libvirt_backup_system.nbd_probe.run",
        lambda args, *, check=True, env=None: CommandResult(args, 0, "2\n", ""),
    )
    assert domain_socket_path("qemu:///system", "alpha") is None


def test_virtnbdbackup_socket_args_returns_empty_when_no_path(monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.domain_socket_path", lambda uri, name: None)
    assert virtnbdbackup_socket_args("qemu:///system", "alpha") == []


def test_virtnbdbackup_socket_args_unlinks_stale_and_returns_flag(monkeypatch, tmp_path: Path) -> None:
    sock = tmp_path / "vnbd.sock"
    sock.write_bytes(b"stale")
    monkeypatch.setattr("libvirt_backup_system.nbd_probe.domain_socket_path", lambda uri, name: sock)
    assert virtnbdbackup_socket_args("qemu:///system", "alpha") == ["-f", str(sock)]
    assert not sock.exists()


def test_socket_args_flow_into_backup_vm_argv(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    from libvirt_backup_system.backup import backup_vm
    from tests.unit.conftest import virtnbdbackup_fake_success

    backup_config.values.update({"BACKUP_COMPRESS": "true"})
    sock = tmp_path / "domain-2-alpha" / "vnbd.sock"
    calls: list[list[str]] = []

    def fake(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return virtnbdbackup_fake_success(args, check=check, env=env)

    monkeypatch.setattr("libvirt_backup_system.backup.virtnbdbackup_socket_args", lambda uri, name: ["-f", str(sock)])
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake)
    assert backup_vm(backup_config, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    idx = calls[0].index("-f")
    assert calls[0][idx : idx + 2] == ["-f", str(sock)]
