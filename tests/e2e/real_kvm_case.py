from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SESSION_URI = "qemu:///session"
PROBE_BINARIES = ("virsh", "virtnbdbackup", "virtnbdrestore", "qemu-img")


def real_kvm_skip_reason() -> str | None:
    if platform.system() != "Linux":
        return "host is not Linux"
    if not Path("/dev/kvm").exists():
        return "/dev/kvm is missing"
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "/dev/kvm is not readable+writable by the current user"
    for binary in PROBE_BINARIES:
        if not shutil.which(binary):
            return f"required binary missing: {binary}"
    probe = subprocess.run(["virsh", "-c", SESSION_URI, "uri"], text=True, capture_output=True, check=False)
    if probe.returncode != 0 or SESSION_URI not in probe.stdout:
        return f"virsh cannot connect to {SESSION_URI}: {probe.stderr.strip() or probe.stdout.strip()}"
    return None


def _run(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(args, text=True, capture_output=True, env=merged, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(args)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def _virsh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["virsh", "-c", SESSION_URI, *args], check=check)


def _session_domain_names() -> list[str]:
    proc = _virsh(["list", "--all", "--name"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _domain_xml(name: str, disk_path: Path) -> str:
    return (
        f"<domain type='kvm'>\n"
        f"  <name>{name}</name>\n"
        f"  <uuid>{uuid.uuid4()}</uuid>\n"
        f"  <memory unit='MiB'>32</memory>\n"
        f"  <currentMemory unit='MiB'>32</currentMemory>\n"
        f"  <vcpu placement='static'>1</vcpu>\n"
        f"  <os><type arch='x86_64' machine='pc'>hvm</type></os>\n"
        f"  <features><acpi/></features>\n"
        f"  <on_poweroff>destroy</on_poweroff>\n"
        f"  <on_reboot>destroy</on_reboot>\n"
        f"  <on_crash>destroy</on_crash>\n"
        f"  <devices>\n"
        f"    <emulator>/usr/bin/qemu-system-x86_64</emulator>\n"
        f"    <disk type='file' device='disk'>\n"
        f"      <driver name='qemu' type='qcow2'/>\n"
        f"      <source file='{disk_path}'/>\n"
        f"      <target dev='vda' bus='virtio'/>\n"
        f"    </disk>\n"
        f"  </devices>\n"
        f"</domain>\n"
    )


def _define_domain(work: Path, name: str, *, running: bool) -> Path:
    disk = work / f"{name}.qcow2"
    _run(["qemu-img", "create", "-f", "qcow2", str(disk), "16M"])
    xml_path = work / f"{name}.xml"
    xml_path.write_text(_domain_xml(name, disk), encoding="utf-8")
    _virsh(["define", str(xml_path)])
    if running:
        _virsh(["start", name])
    return disk


def _teardown_domains(names: Iterable[str]) -> None:
    for name in names:
        _virsh(["destroy", name], check=False)
        # virtnbdbackup leaves a per-domain libvirt checkpoint (named
        # ``virtnbdbackup.0``) on every successful backup. libvirt refuses
        # ``undefine`` while metadata-only checkpoints exist, so add
        # ``--checkpoints-metadata`` to clear them along with the domain. This
        # only removes libvirt metadata; backup-side checkpoint files live
        # under the workdir and are removed by ``shutil.rmtree``.
        _virsh(["undefine", "--checkpoints-metadata", name], check=False)


def _write_config(
    config_path: Path,
    *,
    backup_path: Path,
    host_id: str,
    blacklist: list[str],
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"LIBVIRT_URI={SESSION_URI}",
        f"BACKUP_PATH={backup_path}",
        f"HOST_ID={host_id}",
        f"VM_BLACKLIST={' '.join(blacklist)}",
        "BACKUP_COMPRESS=false",
        "BACKUP_REQUIRE_NFS_MOUNT=false",
        "SPACE_MARGIN_PERCENT=20",
        "INACTIVE_COPY_EVERY_RUN=false",
        "BACKUP_ESTIMATE_GB_PER_VM=0.001",
        "REQUIRE_ROOT=false",
        "",
    ]
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _json_lines(output: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for line in output.splitlines():
        text = line.strip()
        if not text:
            continue
        records.append(json.loads(text))
    return records


def _assert_backup_layout(backup_path: Path, host_id: str, vm: str) -> Path:
    vm_root = backup_path / host_id / vm
    months = list(vm_root.glob("????-??"))
    assert months, f"no month directory under {vm_root}"
    stamps = sorted(months[0].glob("*T*Z"))
    assert stamps, f"no timestamp directory under {months[0]}"
    backup = stamps[-1]
    data_files = list(backup.glob("vda.*.data"))
    assert data_files, f"no virtnbdbackup data file under {backup}: {list(backup.iterdir())}"
    return backup


def _assert_inactive_marker(backup_path: Path, host_id: str, vm: str) -> None:
    months = list((backup_path / host_id / vm).glob("????-??"))
    assert months, f"no month directory for inactive VM {vm}"
    marker = months[0] / ".inactive-copy-complete"
    assert marker.is_file(), f"inactive marker missing under {months[0]}"


def _run_scenario(work: Path, running_name: str, inactive_name: str) -> None:
    backup_path = work / "backup"
    backup_path.mkdir()
    prefix = work / "root"
    host_id = "lbs-e2e"

    install_env = {"LIBVIRT_BACKUP_ROOT_PREFIX": str(prefix), "PYTHONPATH": str(ROOT)}
    _run(
        [sys.executable, "-m", "libvirt_backup_system", "--prefix", str(prefix), "install"],
        env=install_env,
    )
    bin_path = prefix / "usr/local/bin/libvirt-backup-system"
    config_path = prefix / "etc/libvirt-backup-system/libvirt-backup.env"
    assert bin_path.exists(), f"installer did not create {bin_path}"

    # Snapshot any session VMs that already existed before our run and add them
    # to VM_BLACKLIST so the orchestrator only touches the two test domains.
    pre_existing = [name for name in _session_domain_names() if name not in {running_name, inactive_name}]
    _write_config(config_path, backup_path=backup_path, host_id=host_id, blacklist=pre_existing)

    check = _run([str(bin_path), "check"])
    records = _json_lines(check.stdout)
    assert any(r.get("message") == "preflight passed" for r in records), f"preflight did not pass: {records}"

    vms_proc = _run([str(bin_path), "list-vms", "--json"])
    listed = json.loads(vms_proc.stdout)
    names = {vm["name"] for vm in listed}
    assert running_name in names, f"running domain missing from list-vms: {listed}"
    assert inactive_name in names, f"inactive domain missing from list-vms: {listed}"
    states = {vm["name"]: vm["state"] for vm in listed}
    assert states[running_name] == "running", states
    assert states[inactive_name] == "shut off", states

    _run([str(bin_path), "run"])
    backup_dir_running = _assert_backup_layout(backup_path, host_id, running_name)
    backup_dir_inactive = _assert_backup_layout(backup_path, host_id, inactive_name)
    _assert_inactive_marker(backup_path, host_id, inactive_name)

    _run([str(bin_path), "verify"])

    # Inactive idempotency: a second run must reuse the existing copy, so no
    # new timestamp directory is created under the inactive VM's month dir.
    inactive_stamps_before = sorted(backup_dir_inactive.parent.glob("*T*Z"))
    running_stamps_before = sorted(backup_dir_running.parent.glob("*T*Z"))
    _run([str(bin_path), "run"])
    inactive_stamps_after = sorted(backup_dir_inactive.parent.glob("*T*Z"))
    running_stamps_after = sorted(backup_dir_running.parent.glob("*T*Z"))
    assert (
        inactive_stamps_after == inactive_stamps_before
    ), f"inactive marker did not prevent recopy: before={inactive_stamps_before} after={inactive_stamps_after}"
    assert len(running_stamps_after) == len(running_stamps_before) + 1, (
        f"running VM second run did not produce a new timestamp directory: "
        f"before={running_stamps_before} after={running_stamps_after}"
    )

    _run(
        [sys.executable, "-m", "libvirt_backup_system", "--prefix", str(prefix), "uninstall"],
        env=install_env,
    )
    assert not bin_path.exists(), "uninstall did not remove the CLI wrapper"


def main() -> int:
    reason = real_kvm_skip_reason()
    if reason:
        print(f"real KVM e2e cannot run: {reason}", file=sys.stderr)
        return 1
    tag = uuid.uuid4().hex[:8]
    running_name = f"lbs-e2e-{tag}-running"
    inactive_name = f"lbs-e2e-{tag}-inactive"
    work = Path(tempfile.mkdtemp(prefix="lbs-e2e-real-"))
    print(f"real KVM e2e: work={work} running={running_name} inactive={inactive_name}", flush=True)
    try:
        _define_domain(work, running_name, running=True)
        _define_domain(work, inactive_name, running=False)
        _run_scenario(work, running_name, inactive_name)
        print("real KVM e2e: PASS", flush=True)
        return 0
    finally:
        _teardown_domains((running_name, inactive_name))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
