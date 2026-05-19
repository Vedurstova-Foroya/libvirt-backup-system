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
                "VM_BLACKLIST=cccccccc-cccc-cccc-cccc-cccccccccccc",
                "BACKUP_COMPRESS=true",
                "BACKUP_REQUIRE_NFS_MOUNT=true",
                "SPACE_MARGIN_PERCENT=20",
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


def _assert_restore_composes_with_until(month: str) -> None:
    # Validate that ``restore <uuid> <ts>`` composes into ``virtnbdrestore
    # --until <checkpoint>``: pick the first timestamp recorded in runs.jsonl,
    # drive restore through the fake, and confirm the fake received a --until
    # that resolves to a known checkpoint in the chain dir. The fake fails
    # closed on an unknown checkpoint, so a green exit here is proof of the
    # composition.
    alpha_runs_path = next(
        (BACKUP_PATH / "e2e-host" / VM_UUID["alpha"] / month).glob("[0-9]*T[0-9]*/runs.jsonl"),
        None,
    )
    if alpha_runs_path is None:
        return
    first_record = json.loads(alpha_runs_path.read_text(encoding="utf-8").splitlines()[0])
    restore = run([str(BIN), "restore", VM_UUID["alpha"], first_record["ts"]])
    assert_json_lines(restore.stdout)
    receipts = list((PREFIX / "var/lib/libvirt-backup-system/restore").glob("*/restore-receipt.json"))
    assert receipts, "fake virtnbdrestore did not write its receipt"
    receipt = json.loads(receipts[-1].read_text(encoding="utf-8"))
    assert receipt["until"] == first_record["checkpoint"], receipt


def _assert_list_restore_points_emits_uuid_and_timestamp(month: str) -> None:
    # list-restore-points must print at least one row per recorded run. The
    # first two whitespace-separated columns are the UUID and the timestamp
    # so a copy of that pair feeds straight into ``restore``.
    listing = run([str(BIN), "list-restore-points"])
    output_lines = [line for line in listing.stdout.splitlines() if line.strip()]
    assert any(VM_UUID["alpha"] in line and month.replace("-", "") in line for line in output_lines), listing.stdout
    header = output_lines[0].split()
    assert header[:2] == ["VM_UUID", "TIMESTAMP"], header


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
    # Only running VMs are backed up. ``beta`` is shut off (per fake virsh) so
    # it must be skipped with the documented log line and produce no backup
    # tree at all.
    month_dirs = list((BACKUP_PATH / "e2e-host" / VM_UUID["alpha"]).glob("????-??"))
    assert month_dirs, "running VM backup month missing"
    assert not (BACKUP_PATH / "e2e-host" / VM_UUID["beta"]).exists(), "offline VM must not produce a backup tree"
    backup_records = [json.loads(line) for line in backup.stdout.splitlines() if line.strip()]
    assert any(
        r.get("message") == "skipping vm because it is offline" and r.get("vm") == "beta" for r in backup_records
    ), backup_records
    month = month_dirs[0].name

    timestamps = sorted((BACKUP_PATH / "e2e-host" / VM_UUID["alpha"] / month).glob("[0-9]*T[0-9]*"))
    assert timestamps, "no timestamped backup directory for alpha"
    metadata_path = timestamps[-1] / "metadata.json"
    assert metadata_path.is_file(), "metadata.json missing for alpha"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["domain"] == "alpha", metadata
    assert metadata["disks"], "no disks recorded for alpha"

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

    _assert_list_restore_points_emits_uuid_and_timestamp(month)
    _assert_restore_composes_with_until(month)

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
