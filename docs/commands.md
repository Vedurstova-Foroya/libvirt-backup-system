# Command reference

## `install`

Installs the package copy, wrapper script, config file, and systemd units when `BACKUP_PATH` is configured. It does not enable the timer; run `check` and then `start` after editing the environment file.

```sh
sudo libvirt-backup-system install
```

When run via the installed wrapper the package files at `/opt/libvirt-backup-system` are kept as-is (refreshing them mid-execute would delete the source being copied). The wrapper, env file, and systemd units are still refreshed, but `start` is the explicit command for applying `BACKUP_PATH` or `SYSTEMD_ON_CALENDAR` changes and activating the timer. To pick up new package code, run `install` from a source checkout instead:

```sh
sudo python3 -m libvirt_backup_system install
```

## `uninstall`

Removes installed program files and systemd units. Config, state, logs, and backups are preserved unless purge flags are passed.

```sh
sudo libvirt-backup-system uninstall
sudo libvirt-backup-system uninstall --purge-config --purge-state --purge-logs
```

## `check` / `preflight`

Validates config, required binaries, root execution policy, VM discovery, backup path writability, and estimated free space.

```sh
sudo libvirt-backup-system check
```

## `doctor`

Diagnoses install registration, runtime state, and the same preflight surface that `check` covers. Specifically, `doctor` is a superset of `check` — it runs the full preflight layer (config, binaries, root execution policy, VM discovery, scratch dir, NBD probe, backup-path writability and space estimate) and then appends:

- The wrapper script, package directory, and config file are in place.
- All three systemd unit files exist on disk with content matching what a fresh `install` would render (catches drift after editing the env file without re-running install).
- The timer is enabled and active.
- The most recent `libvirt-backup-system.service` run completed cleanly (a stale `Result` other than `success` means the last fire failed).

Any failure from either layer is reported under the same `doctor failed` event so operators see one combined report. Use `check` when you only want the pre-run preflight; use `doctor` when you also need install/registration/last-run health.

```sh
sudo libvirt-backup-system doctor
```

## `start`

Installs or refreshes the systemd unit files from the current environment file, reloads systemd, and enables/starts `libvirt-backup-system.timer`. Use this after `install`, editing `/etc/libvirt-backup-system/libvirt-backup.env`, and `check`; use `run` when you want to execute a backup immediately.

```sh
sudo libvirt-backup-system start
```

## `run`

Runs preflight, acquires the run lock, and backs up selected VMs. Running VMs build a per-month incremental chain: the first run each calendar month writes a `-l full` into a new chain directory, subsequent runs in the same month append `-l inc` snapshots into the same chain. Inactive (shut-off) VMs keep their `-l copy` semantics with a per-month freshness marker.

When `BACKUP_CLEANUP_ON_RUN=true` (the default) the run finishes by pruning month directories older than `BACKUP_RETENTION_MONTHS`. Pruning failure does not roll back successful backups — the run returns the higher of the backup and prune exit codes.

```sh
sudo libvirt-backup-system run
```

## `status`

Prints `systemctl status` for the installed timer and service. Output is the raw human-readable systemctl output (not JSON), so the next-fire time, last-run result, and any recent journal lines are visible at a glance. Exit code is the worst (highest) systemctl return code across the two units, so unloaded units propagate as failure.

```sh
sudo libvirt-backup-system status
```

## `list-vms`

Lists selected VMs after applying `VM_BLACKLIST`.

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system list-vms --json
sudo libvirt-backup-system list-vms --include-blacklisted
```

## `verify`

Runs `virtnbdrestore -a verify` against discovered backup directories.

```sh
sudo libvirt-backup-system verify
sudo libvirt-backup-system verify --vm my-vm
```

## `restore`

Reconstructs a VM into an empty staging directory using `virtnbdrestore -a restore`. The two required arguments are the VM identifier and the output directory; `--at` pins recovery to a target time and is the only knob beyond that.

```sh
# Latest snapshot for ``my-vm``
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm

# Chain whose start time is at-or-before May 7th, 10:30 UTC
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm --at 2026-05-07T10:30:00

# Pin to the exact chain dir name (copied from a directory listing)
sudo libvirt-backup-system restore --vm <uuid> --output /var/tmp/restore/my-vm --at 20260507T101112
```

`--vm` accepts either a VM name (resolved via `virsh domuuid`) or a UUID directly. `--output` must either not exist or be an empty directory — restore refuses to overwrite existing data so an aborted recovery cannot blend with leftover files.

`--at` accepts:

- `YYYY-MM-DD` — interpreted as midnight UTC of that day; selects the latest chain whose start is at-or-before midnight. Note that `--at 2026-05-07` will not pick a chain started later that same day — use `--at 2026-05-07T23:59:59` to mean "end-of-day May 7th".
- `YYYY-MM-DDTHH:MM:SS` (or `YYYY-MM-DD HH:MM:SS`) — UTC unless a timezone offset is included (e.g. `2026-05-07T13:11:12+03:00`).
- `YYYYMMDDTHHMMSS` — the compact form chain directories themselves use.

If omitted, the latest snapshot across all months wins. If `--at` is *earlier* than every chain start, restore exits with `restore --at is earlier than the oldest backup` rather than silently restoring the oldest available chain.

`--at` resolves at **per-run (checkpoint) granularity**. Each successful backup writes a `runs.jsonl` record into its chain directory mapping the run's UTC timestamp to the new `virtnbdbackup` checkpoint. Restore picks the chain whose start is at-or-before `--at`, then within that chain selects the latest recorded run at-or-before `--at` and passes `virtnbdrestore --until <checkpoint>` so replay stops exactly there. A May 1st chain with daily incrementals through May 20th plus `--at 2026-05-10T12:00:00` recovers the May 10th state — not May 20th and not May 1st.

> **Legacy fallback.** Chain directories created before this feature shipped have no `runs.jsonl` at all. For those, `--at` still selects the right chain, but the replay runs to chain end (the old chain-end semantics) because there is no per-run record to target. A single fresh backup of the affected VM begins recording new runs going forward; the prior chain remains chain-end-only until a new chain starts.
>
> Chains where `runs.jsonl` *is* present but unusable — truncated by power loss, hand-edited into invalid JSON, or with no record at-or-before `--at` — are **not** treated as legacy. Falling back to chain end there would silently restore a newer state than the operator asked for, so `restore` refuses with `restore --at has no matching run record` and the operator either fixes the file or picks a different `--at`.

### How chains map to snapshots

Backups live under:

```
BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/
```

`<chain-id>` is the UTC timestamp of the first run that opened the chain (e.g. `20260501T023000`). Inside the chain dir, `virtnbdbackup` writes:

- One `*-full-*.data` from the first run that opened the chain (`-l full`).
- Subsequent runs the same month append `*-inc-*.data` snapshots and `<checkpoint>.checkpoint` files to the **same** chain dir (`-l inc`); the dir name does not change.
- Inactive VMs use `-l copy` and get a fresh chain dir per run (no incremental tail).
- `metadata.json` describing the disks and checkpoints (written by virtnbdbackup).
- `runs.jsonl` — one JSON line per successful run (`{"ts": "<YYYYMMDDTHHMMSS>", "checkpoint": "<name>"}`) used by `restore --at` to map a target time to the specific `virtnbdrestore --until` checkpoint.
- `<vm-name>.name` — empty marker we drop so operators can `find -name '<name>.name'` to map a current VM name back to its UUID dir.

A new chain (a new full) is started when any of these happen:

- A new calendar month begins (the run lands under a fresh `<yyyy-mm>/`).
- The VM's libvirt XML fingerprint changes (e.g. disk added).
- The previous chain dir was deleted out-of-band.

Restore's `--at` selection first picks a chain dir by its **chain start time** — the `<chain-id>` itself — and then picks a per-run **checkpoint** inside that chain by reading the `runs.jsonl` written alongside the backup files. `virtnbdrestore -a restore -i <chain-dir> -o <output> --until <checkpoint>` then replays the full plus only the incrementals up to that checkpoint, so the recovered state is the exact state captured by that backup run. Legacy chains without `runs.jsonl` omit `--until` and replay end-to-end.

The restore command holds the same run-lock as `run` to avoid reading a chain dir that a concurrent backup is still writing into. See [Manual restore process](manual-restore.md) for the lower-level recovery procedure (useful when the source backup must first be staged onto local storage).

## Retention

Old month directories are pruned automatically at the end of every successful `run` when `BACKUP_CLEANUP_ON_RUN=true` (the default). The number of most-recent calendar months to keep is `BACKUP_RETENTION_MONTHS` (default `12`, roughly one year); `0` disables pruning entirely. Pruning is per-VM and only touches `<vm-uuid>/<yyyy-mm>/` directories — foreign files dropped under a VM dir are left alone. The most recent month dir is always preserved even if retention math would otherwise drop it.
