"""End-to-end checks of the dynamic fish completion for ``restore``.

The completion functions in
libvirt_backup_system/data/libvirt-backup-system.fish are shell code, so we
exercise them by running fish itself with a stubbed ``libvirt-backup-system``
binary on PATH. The tests skip cleanly when fish is not installed so that CI
runners without fish do not fail the suite (the gate workflow installs fish
before running the gate).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import libvirt_backup_system

COMPLETION_FILE = Path(libvirt_backup_system.__file__).resolve().parent / "data" / "libvirt-backup-system.fish"
FIXTURE_OUTPUT = (
    "source-host-id  vm-uuid  timestamp  run-id  vm-name\n"
    "host1           aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa  20260101T000000  "
    "11111111-1111-1111-1111-111111111111  alpha vm\n"
    "host1           aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa  20260102T030000  "
    "22222222-2222-2222-2222-222222222222  alpha vm\n"
    "host2           bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb  20260105T000000  "
    "33333333-3333-3333-3333-333333333333  beta\n"
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


def _run_fish(fish: str, bindir: Path, completion_line: str, *, cwd: Path | None = None) -> str:
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
        f"set -gx XDG_CACHE_HOME {bindir.parent / 'cache'}\n"
        "source /usr/share/fish/completions/sudo.fish 2>/dev/null; or true\n"
        f"source {COMPLETION_FILE}\n"
        f"complete -C '{completion_line}'\n"
    )
    result = subprocess.run([fish, "--no-config"], input=script, capture_output=True, text=True, check=True, cwd=cwd)
    return result.stdout


def test_uuid_completion_lists_distinct_uuids_with_host_id(tmp_path: Path) -> None:
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
    # Description carries VM name, source host, and restore-point count.
    assert "alpha vm - host1 (2 restore points)" in out
    assert "beta - host2 (1 restore points)" in out


def test_uuid_completion_ignores_verbose_flag_before_uuid(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(fish, bindir, "libvirt-backup-system restore -v ")
    values = [line.split("\t", 1)[0] for line in out.splitlines()]
    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in values
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in values


@pytest.mark.parametrize("option", ["--host-id", "--run-id"])
def test_restore_disambiguation_options_do_not_complete_cwd_files(tmp_path: Path, option: str) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    (tmp_path / "cwd-file-should-not-complete").write_text("", encoding="utf-8")

    out = _run_fish(fish, bindir, f"libvirt-backup-system restore {option} ", cwd=tmp_path)

    assert "cwd-file-should-not-complete" not in out


@pytest.mark.parametrize("command_line", ["libvirt-backup-system du ", "sudo libvirt-backup-system du "])
def test_du_first_arg_completion_lists_hosts_and_uuids(tmp_path: Path, command_line: str) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(fish, bindir, command_line)
    values = [line.split("\t", 1)[0] for line in out.splitlines()]

    assert {"host1", "host2"} <= set(values)
    assert {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"} <= set(values)
    assert values.count("host1") == 1
    assert "alpha vm - host1 (2 restore points)" in out


def test_du_second_arg_completion_filters_vms_to_host(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(fish, bindir, "libvirt-backup-system du host1 ")
    values = [line.split("\t", 1)[0] for line in out.splitlines()]

    assert "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in values
    assert "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" not in values
    assert "alpha vm (2 restore points)" in out


@pytest.mark.parametrize(
    "command_line",
    [
        "libvirt-backup-system du aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
        "sudo libvirt-backup-system du aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    ],
)
def test_du_completion_after_vm_uuid_hides_subcommands(tmp_path: Path, command_line: str) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)

    out = _run_fish(fish, bindir, command_line)
    values = {line.split("\t", 1)[0] for line in out.splitlines()}

    assert values.isdisjoint({"change-password", "install", "restore", "run", "verify"})


def test_du_positional_completion_does_not_complete_cwd_files(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    (tmp_path / "cwd-file-should-not-complete").write_text("", encoding="utf-8")

    out = _run_fish(fish, bindir, "libvirt-backup-system du host1 ", cwd=tmp_path)

    assert "cwd-file-should-not-complete" not in out


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


def test_timestamp_completion_ignores_verbose_flag_before_uuid(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "libvirt-backup-system restore --verbose aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    values = [line.split("\t", 1)[0] for line in out.splitlines()]
    assert "20260101T000000" in values
    assert "20260102T030000" in values
    assert "20260105T000000" not in values


def test_timestamp_completion_carries_host_and_run_id_description(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(
        fish,
        bindir,
        "libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa ",
    )
    # The description carries source host and RUN_ID from list-restore-points for
    # diagnostics while the completion value stays copy-pasteable TIMESTAMP.
    rows = {line.split("\t", 1)[0]: line.split("\t", 1)[1] for line in out.splitlines() if "\t" in line}
    assert rows["20260101T000000"] == "host1 11111111-1111-1111-1111-111111111111 alpha vm"
    assert rows["20260102T030000"] == "host1 22222222-2222-2222-2222-222222222222 alpha vm"


def test_restore_completion_uses_cached_restore_points(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    first = _run_fish(fish, bindir, "libvirt-backup-system restore ")
    (bindir / "libvirt-backup-system").write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")

    second = _run_fish(fish, bindir, "libvirt-backup-system restore ")

    assert "alpha vm - host1 (2 restore points)" in first
    assert "alpha vm - host1 (2 restore points)" in second


def test_restore_completion_refreshes_stale_cache(tmp_path: Path) -> None:
    fish = _require_fish()
    bindir = _seed_fakes(tmp_path)
    out = _run_fish(fish, bindir, "libvirt-backup-system restore ")
    assert "alpha vm - host1 (2 restore points)" in out
    cache = tmp_path / "cache/libvirt-backup-system/restore-points.tsv"
    old = time.time() - 10
    os.utime(cache, (old, old))
    (tmp_path / "fixture.txt").write_text(
        FIXTURE_OUTPUT + "host3           cccccccc-cccc-cccc-cccc-cccccccccccc  20260106T000000  "
        "44444444-4444-4444-4444-444444444444  gamma\n",
        encoding="utf-8",
    )

    refreshed = _run_fish(fish, bindir, "libvirt-backup-system restore ")

    assert "gamma - host3 (1 restore points)" in refreshed


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
