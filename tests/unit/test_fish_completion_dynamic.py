"""End-to-end checks of the dynamic fish completion for ``restore``.

The completion functions in
libvirt_backup_system/data/libvirt-backup-system.fish are shell code, so we
exercise them by running fish itself with a stubbed ``libvirt-backup-system``
binary on PATH. The tests skip cleanly when fish is not installed so that CI
runners without fish do not fail the suite (the gate workflow installs fish
before running the gate).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import libvirt_backup_system

COMPLETION_FILE = Path(libvirt_backup_system.__file__).resolve().parent / "data" / "libvirt-backup-system.fish"
FIXTURE_OUTPUT = (
    "VM_UUID                               TIMESTAMP        VM_NAME  HOST_ID  KIND  MONTH    CHAIN_ID\n"
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa  20260101T000000  alpha    host1    full  2026-01  20260101T000000\n"
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa  20260102T030000  alpha    host1    inc   2026-01  20260101T000000\n"
    "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb  20260105T000000  beta     host1    full  2026-01  20260105T000000\n"
)


def _require_fish() -> str:
    fish = shutil.which("fish")
    if fish is None:
        pytest.skip("fish is not installed on this host")
    return fish


def _seed_fakes(tmp_path: Path) -> Path:
    """Drop a sudo and libvirt-backup-system stub into ``tmp_path/bin``.

    The sudo stub exits non-zero so the completion's ``sudo -n`` short-circuits
    into the non-sudo fallback; the libvirt-backup-system stub prints the
    fixture rows that the awk pipelines parse. Both fakes are shell scripts so
    they need no Python and start fast (fish would otherwise spend most of the
    test wall-clock waiting on python startup).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sudo = bindir / "sudo"
    sudo.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    sudo.chmod(0o755)
    lbs = bindir / "libvirt-backup-system"
    fixture_path = tmp_path / "fixture.txt"
    fixture_path.write_text(FIXTURE_OUTPUT, encoding="utf-8")
    lbs.write_text(f"#!/bin/sh\ncat {fixture_path}\n", encoding="utf-8")
    lbs.chmod(0o755)
    return bindir


def _run_fish(fish: str, bindir: Path, completion_line: str) -> str:
    # ``--no-config`` keeps fish from auto-loading any older copy of the
    # completion file from /usr/share/fish/vendor_completions.d/ — the test
    # must always read the in-tree script, not whatever the last ``install``
    # run left on disk. We also explicitly source the system sudo completion
    # so the sudo-dispatched paths under test resolve the same way they do
    # in an interactive operator shell (under --no-config the sudo
    # completion is not auto-loaded otherwise). ``set -gx PATH`` then
    # overwrites fish's PATH so the operator's own libvirt-backup-system
    # (often under ~/.local/bin via fish_user_paths) cannot shadow the fake.
    script = (
        f"set -gx PATH {bindir} /usr/bin /bin\n"
        "source /usr/share/fish/completions/sudo.fish 2>/dev/null; or true\n"
        f"source {COMPLETION_FILE}\n"
        f"complete -C '{completion_line}'\n"
    )
    result = subprocess.run([fish, "--no-config"], input=script, capture_output=True, text=True, check=True)
    return result.stdout


def test_uuid_completion_lists_distinct_uuids_with_vm_name(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(fish, bindir, "libvirt-backup-system restore ")
    lines = out.splitlines()
    # Fish formats completions as ``<value>\t<description>``. One row per
    # distinct UUID: the duplicate alpha rows in the fixture must dedupe.
    values = [line.split("\t", 1)[0] for line in lines]
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in values
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in values
    assert values.count("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa") == 1
    # Description carries the VM name plus a backup count. We deliberately
    # avoid showing full/inc kind here because the first row for every UUID
    # is the chain full, which used to mislead operators into thinking the
    # menu listed only full backups instead of VMs.
    assert "alpha (2 backups)" in out
    assert "beta (1 backups)" in out
    assert "full" not in out
    assert "inc" not in out


def test_timestamp_completion_filters_to_chosen_uuid(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    values = [line.split("\t", 1)[0] for line in out.splitlines()]
    # Both alpha rows must surface; beta's row must NOT (its UUID does not
    # match the one the operator already typed).
    assert "20260101T000000" in values
    assert "20260102T030000" in values
    assert "20260105T000000" not in values


def test_timestamp_completion_carries_kind_description(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    # The kind ("full" / "inc") rides as the description so the operator can
    # spot the chain start versus mid-chain points in the menu.
    rows = {line.split("\t", 1)[0]: line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line}
    assert rows["20260101T000000"] == "full"
    assert rows["20260102T030000"] == "inc"


def test_timestamp_completion_orders_newest_first(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    # ``sort -r`` puts the most recent timestamp at the top so the typical
    # "restore to the latest point" intent is one arrow-down (or zero) away.
    values = [line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line]
    assert values == sorted(values, reverse=True)


def test_timestamp_completion_orders_newest_first_under_sudo(tmp_path: Path) -> None:
    # fish's stock sudo completion dispatcher re-sorts the candidate list
    # alphabetically (its own ``complete -c sudo`` registration carries no
    # ``-k`` flag), which silently reversed our newest-first order to
    # oldest-first for any operator typing ``sudo libvirt-backup-system
    # restore <uuid> <TAB>``. The completion file mirrors the two restore-
    # stage registrations directly under ``sudo`` so our ``-k`` flag wins.
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "sudo libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    values = [line.split("\t", 1)[0] for line in out.splitlines() if "\t" in line]
    assert values, f"sudo path produced no candidates: {out!r}"
    assert values == sorted(values, reverse=True), values
