from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PREFIX = Path("/tmp/lbs-root")
BIN = PREFIX / "usr/local/bin/libvirt-backup-system"
CONFIG = PREFIX / "etc/libvirt-backup-system/libvirt-backup.env"
BACKUP_PATH = Path("/mnt/qnap-backups")

# Keep in sync with the fake virsh's domuuid table — backups now live under
# the libvirt UUID, not the VM name.
VM_UUID = {
    "alpha": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    "beta": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
}


def run(args: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(args, text=True, capture_output=True, env=merged, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(args)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def write_config() -> None:
    BACKUP_PATH.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        "\n".join(
            [
                "LIBVIRT_URI=test:///default",
                f"BACKUP_PATH={BACKUP_PATH}",
                "HOST_ID=e2e-host",
                "VM_BLACKLIST=ignore-me",
                "BACKUP_COMPRESS=true",
                "BACKUP_REQUIRE_NFS_MOUNT=true",
                "SPACE_MARGIN_PERCENT=20",
                "INACTIVE_COPY_EVERY_RUN=false",
                "BACKUP_ESTIMATE_GB_PER_VM=0.001",
                "REQUIRE_ROOT=true",
                "",
            ]
        ),
        encoding="utf-8",
    )


def assert_json_lines(output: str) -> None:
    records = [json.loads(line) for line in output.splitlines() if line.strip()]
    assert records, "expected JSON logs"
    assert all("level" in record and "message" in record for record in records)


def _assert_restore_at_composes_with_until(month: str) -> None:
    # Validate that restore --at composes into virtnbdrestore --until: pick the
    # first timestamp recorded in runs.jsonl, drive restore through the fake,
    # and confirm the fake received a --until that resolves to a known
    # checkpoint in the chain dir. The fake fails closed on an unknown
    # checkpoint, so a green exit here is proof of the composition.
    alpha_runs_path = next(
        (BACKUP_PATH / "e2e-host" / VM_UUID["alpha"] / month).glob("[0-9]*T[0-9]*/runs.jsonl"),
        None,
    )
    if alpha_runs_path is None:
        return
    restore_target = PREFIX / "restore-out"
    shutil.rmtree(restore_target, ignore_errors=True)
    first_record = json.loads(alpha_runs_path.read_text(encoding="utf-8").splitlines()[0])
    restore = run(
        [str(BIN), "restore", "--vm", "alpha", "--output", str(restore_target), "--at", first_record["ts"]],
    )
    assert_json_lines(restore.stdout)
    receipt_path = restore_target / "restore-receipt.json"
    assert receipt_path.is_file(), "fake virtnbdrestore did not write its receipt"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["until"] == first_record["checkpoint"], receipt


def main() -> int:
    shutil.rmtree(PREFIX, ignore_errors=True)
    shutil.rmtree(BACKUP_PATH / "e2e-host", ignore_errors=True)

    run(["python3", "-m", "libvirt_backup_system", "--prefix", str(PREFIX), "install"])
    assert BIN.exists(), "installed CLI wrapper is missing"
    assert CONFIG.exists(), "installed config is missing"
    write_config()

    preflight = run([str(BIN), "preflight"])
    assert_json_lines(preflight.stdout)

    vms = run([str(BIN), "list-vms", "--json"])
    listed = json.loads(vms.stdout)
    assert [vm["name"] for vm in listed] == ["alpha", "beta"], listed
    assert [vm["uuid"] for vm in listed] == [VM_UUID["alpha"], VM_UUID["beta"]], listed

    low_space = run([str(BIN), "preflight"], check=False, env={"LBS_FAKE_LOW_SPACE": "1"})
    assert low_space.returncode != 0, "low-space preflight should fail"
    assert "insufficient backup space" in low_space.stderr

    backup = run([str(BIN), "run"])
    assert_json_lines(backup.stdout)
    month_dirs = list((BACKUP_PATH / "e2e-host" / VM_UUID["alpha"]).glob("????-??"))
    assert month_dirs, "running VM backup month missing"
    assert list((BACKUP_PATH / "e2e-host" / VM_UUID["beta"]).glob("????-??")), "inactive VM backup month missing"
    month = month_dirs[0].name

    assert (BACKUP_PATH / "e2e-host" / VM_UUID["alpha"] / month).is_dir(), "backup month missing"

    for vm_name in ("alpha", "beta"):
        timestamps = sorted((BACKUP_PATH / "e2e-host" / VM_UUID[vm_name] / month).glob("[0-9]*T[0-9]*"))
        assert timestamps, f"no timestamped backup directory for {vm_name}"
        metadata_path = timestamps[-1] / "metadata.json"
        assert metadata_path.is_file(), f"metadata.json missing for {vm_name}"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata["domain"] == vm_name, metadata
        assert metadata["disks"], f"no disks recorded for {vm_name}"
        checkpoint = metadata["checkpoint"]
        assert (timestamps[-1] / f"{checkpoint}.checkpoint").is_file(), "checkpoint missing"
        # The empty <vm-name>.name marker lets operators map a current name
        # back to its UUID dir via `find -name '<name>.name'`.
        assert (timestamps[-1] / f"{vm_name}.name").is_file(), f"<vm>.name marker missing for {vm_name}"

    # Second run lands in the same month and writes a new checkpoint as an
    # incremental; the restore composition test below needs at least two run
    # records with distinct timestamps so it can resolve --at to a non-
    # chain-end checkpoint. backup.timestamp() is UTC second-precision, so
    # sleep briefly between the two runs to keep the stamps apart.
    import time as _time

    _time.sleep(2)
    second = run([str(BIN), "run"])
    assert_json_lines(second.stdout)

    run([str(BIN), "verify"])

    _assert_restore_at_composes_with_until(month)

    # A failing run must never wipe the chain dir for the failing VM, and must
    # leave older month dirs alone unless retention removes them. Disable the
    # cleanup pass for this assertion so the retention default does not race
    # with our handcrafted old-month dir.
    old = BACKUP_PATH / "e2e-host" / VM_UUID["alpha"] / "1999-01"
    old.mkdir(parents=True)
    failed = run(
        [str(BIN), "run"],
        check=False,
        env={"FAIL_BACKUP_FOR": "alpha", "BACKUP_CLEANUP_ON_RUN": "false"},
    )
    assert failed.returncode != 0, "backup failure should produce non-zero exit"
    assert "backup failed" in failed.stderr
    assert old.exists(), "backup system must never delete existing data on backup failure"
    old.rmdir()

    run(["python3", "-m", "libvirt_backup_system", "--prefix", str(PREFIX), "uninstall"])
    assert not BIN.exists(), "CLI wrapper should be removed by uninstall"
    assert CONFIG.exists(), "config should be preserved by uninstall"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
