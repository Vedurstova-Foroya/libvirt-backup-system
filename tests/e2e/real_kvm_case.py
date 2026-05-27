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
PROBE_BINARIES = ("virsh", "qemu-img", "qemu-nbd", "nbdcopy", "kopia")
KOPIA_PASSWORD = "swordfish-e2e"
HOST_ID = "lbs-e2e"
# Generous bound; the test disk is 16 MiB so anything under 50 MiB proves
# the kopia content-addressed store deduplicated the restored disk against
# the snapshots that produced it.
POST_RESTORE_BLOAT_LIMIT = 50 * 1024 * 1024


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


def _session_domain_uuids() -> list[str]:
    proc = _virsh(["list", "--all", "--uuid"])
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _domain_xml(name: str, disk_path: Path) -> str:
    return (
        f"<domain type='kvm'><name>{name}</name><uuid>{uuid.uuid4()}</uuid>"
        f"<memory unit='MiB'>32</memory><currentMemory unit='MiB'>32</currentMemory>"
        f"<vcpu placement='static'>1</vcpu>"
        f"<os><type arch='x86_64' machine='pc'>hvm</type></os>"
        f"<features><acpi/></features>"
        f"<on_poweroff>destroy</on_poweroff><on_reboot>destroy</on_reboot><on_crash>destroy</on_crash>"
        f"<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>"
        f"<disk type='file' device='disk'>"
        f"<driver name='qemu' type='qcow2'/><source file='{disk_path}'/>"
        f"<target dev='vda' bus='virtio'/></disk></devices></domain>\n"
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
    # Kopia owns chunk lifecycle, so we no longer need --checkpoints-metadata
    # (virtnbdbackup left libvirt checkpoints behind that blocked undefine; the
    # new engine does not create any).
    for name in names:
        _virsh(["destroy", name], check=False)
        _virsh(["undefine", name], check=False)


def _write_config(config_path: Path, *, backup_path: Path, blacklist: list[str]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"LIBVIRT_URI={SESSION_URI}",
        f"BACKUP_PATH={backup_path}",
        f"HOST_ID={HOST_ID}",
        f"VM_BLACKLIST={' '.join(blacklist)}",
        "BACKUP_REQUIRE_NFS_MOUNT=false",
        "SPACE_MARGIN_PERCENT=20",
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


class _KopiaCtx:
    """Bundled kopia connection params for direct CLI probes from the test."""

    def __init__(self, config_file: Path, password_file: Path, cache_dir: Path) -> None:
        self.config_file = config_file
        self.password_file = password_file
        self.cache_dir = cache_dir

    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["KOPIA_PASSWORD"] = self.password_file.read_text(encoding="utf-8").strip()
        env["KOPIA_CACHE_DIRECTORY"] = str(self.cache_dir)
        env["KOPIA_CHECK_FOR_UPDATES"] = "false"
        return env

    def kopia(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _run(["kopia", "--config-file", str(self.config_file), *args], env=self.env())

    def snapshot_count(self, tags: dict[str, str]) -> int:
        args = ["kopia", "--config-file", str(self.config_file), "snapshot", "list", "--all", "--json"]
        for key, value in sorted(tags.items()):
            args.extend(["--tags", f"{key}:{value}"])
        proc = subprocess.run(args, text=True, capture_output=True, env=self.env(), check=False)
        if proc.returncode != 0:
            raise AssertionError(f"kopia snapshot list failed: {proc.stderr}")
        parsed = json.loads(proc.stdout or "[]")
        return sum(1 for r in parsed if all(r.get("tags", {}).get(k) == v for k, v in tags.items()))


def _assert_repo_layout(backup_path: Path) -> Path:
    repo = backup_path / HOST_ID / "kopia-repo"
    sentinel = repo / "kopia.repository.f"
    assert sentinel.is_file(), f"expected kopia repo sentinel at {sentinel}"
    return repo


def _assert_offline_skip_logged(records: list[dict[str, object]], vm_name: str) -> None:
    matched = [r for r in records if r.get("message") == "skipping vm because it is offline" and r.get("vm") == vm_name]
    assert matched, f"expected 'skipping vm because it is offline' log for {vm_name!r}, got {records}"


def _repo_size_bytes(repo_path: Path) -> int:
    return int(_run(["du", "-sb", str(repo_path)]).stdout.split()[0])


def _pick_restore_point(bin_path: Path, vm_uuid: str) -> str:
    proc = _run([str(bin_path), "list-restore-points"])
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[1] == vm_uuid:
            return parts[3]
    raise AssertionError(f"no restore point listed for {vm_uuid} in:\n{proc.stdout}")


def _assert_counts(ctx: _KopiaCtx, vm_uuid: str, *, meta: int, disk: int) -> None:
    """Per the plan: at least ``meta`` meta snapshots + ``disk`` per disk target."""
    got_meta = ctx.snapshot_count({"vm-uuid": vm_uuid, "kind": "meta"})
    assert got_meta >= meta, f"vm-uuid:{vm_uuid} meta snapshots: got {got_meta}, expected >= {meta}"
    got_disk = ctx.snapshot_count({"vm-uuid": vm_uuid, "kind": "disk", "disk": "vda"})
    assert got_disk >= disk, f"vm-uuid:{vm_uuid} disk:vda snapshots: got {got_disk}, expected >= {disk}"


def _install(prefix: Path) -> tuple[Path, _KopiaCtx]:
    args = [sys.executable, "-m", "libvirt_backup_system", "--prefix", str(prefix)]
    _run([*args, "install", f"--kopia-password={KOPIA_PASSWORD}"], env=_install_env(prefix))
    bin_path = prefix / "usr/local/bin/libvirt-backup-system"
    password_file = prefix / "etc/libvirt-backup-system/kopia.pw"
    config_file = prefix / "var/lib/libvirt-backup-system/kopia-configs" / f"{HOST_ID}.config"
    cache_dir = prefix / "var/cache/libvirt-backup-system/kopia"
    assert bin_path.exists(), f"installer did not create {bin_path}"
    assert password_file.exists(), f"installer did not write password file at {password_file}"
    return bin_path, _KopiaCtx(config_file, password_file, cache_dir)


def _install_env(prefix: Path) -> dict[str, str]:
    return {"LIBVIRT_BACKUP_ROOT_PREFIX": str(prefix), "PYTHONPATH": str(ROOT)}


def _run_scenario(work: Path, running_name: str, offline_name: str) -> None:
    backup_path = work / "backup"
    backup_path.mkdir()
    prefix = work / "root"

    bin_path, ctx = _install(prefix)
    config_path = prefix / "etc/libvirt-backup-system/libvirt-backup.env"

    test_uuids = {
        _virsh(["domuuid", running_name]).stdout.strip(),
        _virsh(["domuuid", offline_name]).stdout.strip(),
    }
    pre_existing = [uid for uid in _session_domain_uuids() if uid not in test_uuids]
    _write_config(config_path, backup_path=backup_path, blacklist=pre_existing)

    check = _run([str(bin_path), "check"])
    records = _json_lines(check.stdout)
    assert any(r.get("message") == "preflight passed" for r in records), f"preflight did not pass: {records}"

    listed = json.loads(_run([str(bin_path), "list-vms", "--json"]).stdout)
    names = {vm["name"] for vm in listed}
    assert running_name in names and offline_name in names, f"missing domains in list-vms: {listed}"
    uuids = {vm["name"]: vm["uuid"] for vm in listed}
    running_uuid, offline_uuid = uuids[running_name], uuids[offline_name]

    # First backup: repo materializes, running VM gets one meta + one disk
    # snapshot, offline VM stays absent.
    first_run = _run([str(bin_path), "run"])
    repo = _assert_repo_layout(backup_path)
    _assert_offline_skip_logged(_json_lines(first_run.stdout), offline_name)
    _assert_counts(ctx, running_uuid, meta=1, disk=1)
    assert ctx.snapshot_count({"vm-uuid": offline_uuid}) == 0

    _run([str(bin_path), "verify"])

    # Second backup: still no chain dir, just a fresh kopia snapshot per kind.
    second_run = _run([str(bin_path), "run"])
    _assert_offline_skip_logged(_json_lines(second_run.stdout), offline_name)
    _assert_counts(ctx, running_uuid, meta=2, disk=2)
    assert ctx.snapshot_count({"vm-uuid": offline_uuid}) == 0

    # Delete-restore-rebackup invariant: deleting the local VM, restoring it
    # from kopia, then re-running ``run`` must NOT bloat the repo (the
    # chain-poison era required a fresh full after every restore — kopia's
    # content-addressed store proves dedup against the restored disks).
    timestamp = _pick_restore_point(bin_path, running_uuid)
    size_before = _repo_size_bytes(repo)
    _virsh(["destroy", running_name], check=False)
    _virsh(["undefine", running_name], check=False)
    _run([str(bin_path), "restore", running_uuid, timestamp])
    _virsh(["start", running_name])
    _run([str(bin_path), "run"])
    growth = _repo_size_bytes(repo) - size_before
    assert growth < POST_RESTORE_BLOAT_LIMIT, (
        f"post-restore re-backup added {growth} bytes (limit {POST_RESTORE_BLOAT_LIMIT}); "
        f"dedup against restored disk likely failed"
    )
    _assert_counts(ctx, running_uuid, meta=3, disk=3)

    # Retention pruning: a tight ``keep-latest`` global policy must evict
    # older snapshots when maintenance runs. Drive both directly through
    # kopia so the assertion does not depend on how the wrapper surfaces
    # the policy knob.
    ctx.kopia("policy", "set", "--global", "--keep-latest=1")
    ctx.kopia("maintenance", "run", "--safety=none", "--full")
    after_prune = ctx.snapshot_count({"vm-uuid": running_uuid, "kind": "meta"})
    assert after_prune <= 2, f"retention should prune older meta snapshots; got {after_prune}"

    args = [sys.executable, "-m", "libvirt_backup_system", "--prefix", str(prefix), "uninstall"]
    _run(args, env=_install_env(prefix))
    assert not bin_path.exists(), "uninstall did not remove the CLI wrapper"


def main() -> int:
    reason = real_kvm_skip_reason()
    if reason:
        print(f"real KVM e2e cannot run: {reason}", file=sys.stderr)
        return 1
    tag = uuid.uuid4().hex[:8]
    running_name = f"lbs-e2e-{tag}-running"
    offline_name = f"lbs-e2e-{tag}-offline"
    work = Path(tempfile.mkdtemp(prefix="lbs-e2e-real-"))
    print(f"real KVM e2e: work={work} running={running_name} offline={offline_name}", flush=True)
    try:
        _define_domain(work, running_name, running=True)
        _define_domain(work, offline_name, running=False)
        _run_scenario(work, running_name, offline_name)
        print("real KVM e2e: PASS", flush=True)
        return 0
    finally:
        _teardown_domains((running_name, offline_name))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
