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
                "BACKUP_RETENTION_MONTHS=1",
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

    low_space = run([str(BIN), "preflight"], check=False, env={"LBS_FAKE_LOW_SPACE": "1"})
    assert low_space.returncode != 0, "low-space preflight should fail"
    assert "insufficient backup space" in low_space.stderr

    backup = run([str(BIN), "run"])
    assert_json_lines(backup.stdout)
    month_dirs = list((BACKUP_PATH / "e2e-host/alpha").glob("????-??"))
    assert month_dirs, "running VM backup month missing"
    assert list((BACKUP_PATH / "e2e-host/beta").glob("????-??")), "inactive VM backup month missing"
    month = month_dirs[0].name

    assert (BACKUP_PATH / "e2e-host/alpha" / month).is_dir(), "backup month missing"

    old = BACKUP_PATH / "e2e-host/alpha" / "1999-01"
    old.mkdir(parents=True)
    run([str(BIN), "cleanup"])
    assert not old.exists(), "old backup month was not cleaned"

    run([str(BIN), "verify"])

    failed = run([str(BIN), "run"], check=False, env={"FAIL_BACKUP_FOR": "alpha"})
    assert failed.returncode != 0, "backup failure should produce non-zero exit"
    assert "backup failed" in failed.stderr

    run(["python3", "-m", "libvirt_backup_system", "--prefix", str(PREFIX), "uninstall"])
    assert not BIN.exists(), "CLI wrapper should be removed by uninstall"
    assert CONFIG.exists(), "config should be preserved by uninstall"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
