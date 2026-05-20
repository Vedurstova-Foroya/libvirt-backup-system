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

Installs or refreshes the systemd unit files from the current environment file, reloads systemd, and enables/starts `libvirt-backup-system.timer`. This activates the schedule only; it does not run a backup immediately. Use this after `install`, editing `/etc/libvirt-backup-system/libvirt-backup.env`, and `check`; use `run` when you want to execute a manual backup.

```sh
sudo libvirt-backup-system start
```

## `run`

Runs preflight, acquires the run lock, and backs up running VMs. Offline VMs are logged as `skipping vm because it is offline` and skipped — only running VMs are backed up. Each backed-up VM builds a per-month incremental chain: the first run each calendar month writes a `-l full` into a new chain directory, subsequent runs in the same month append `-l inc` snapshots into the same chain.

Manual backups require the systemd schedule to have been activated first with a successful `start`. On a systemd host, `run` exits nonzero with a "backup service is not running" error instead of starting an ad-hoc backup when the service/timer has not been installed and activated.

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

Lists selected VMs after applying `VM_BLACKLIST` (UUID-based).

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

## `list-restore-points`

Prints every restorable backup point across all hosts and VMs. The first two
columns are the VM UUID and the per-run timestamp; copy that pair straight
into `restore`. Pipe through `less` or `grep` to filter by host, VM name, or
month.

```sh
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system list-restore-points | grep my-vm
sudo libvirt-backup-system list-restore-points | less -S
```

Each row is one virtnbdbackup run: chains with `runs.jsonl` produce one row per recorded run, legacy chains (predating that file) produce a single chain-end row identified by the chain directory name.

## `restore`

Restores a single backup run identified by the `(vm_uuid, timestamp)` pair from `list-restore-points`. The action is automatic:

- If the backup was taken on this host **and** a libvirt domain with that UUID exists locally, the VM is shut down, undefined, and redefined from the backup (in-place overwrite).
- Otherwise the VM is staged and redefined from the backup XML on this host (turnkey one-click recovery on a different host or after the local VM has been removed).

```sh
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
sudo libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 20260507T101112
```

There are no other flags: the timestamp is the exact per-run target (no rounding, no closest-match). When the backup XML records file-backed disks in a single directory, restored disks are written back to that original disk directory and the VM is defined with the original VM name. Legacy or unsupported layouts fall back to a staging directory under `/var/lib/libvirt-backup-system/restore/<uuid>-<timestamp>/`.

Both modes call `virtnbdrestore -a restore -i <chain> -o <output-dir> [-u <checkpoint>] [--name <vm-name>] -D`. The `-u` argument is omitted for legacy chains (no `runs.jsonl`); for modern chains the checkpoint is looked up by the exact timestamp the operator copied from `list-restore-points`. The `--name` argument preserves the backed-up VM name instead of virtnbdrestore's default `restore_` prefix. The `-D` flag asks `virtnbdrestore` to register the domain in libvirt using the XML stored in the backup so the recovered VM is one `virsh start` away from booting.

By default, restore captures virtnbdrestore's detailed output and prints only summary success/error events. Pass `-v` or `--verbose` to stream the full virtnbdrestore output while the restore runs.

Poisoned chains (those flagged by the backup orchestrator as having a half-written incremental) are refused outright; the operator must either fix the chain or pick a different timestamp.

### Cross-host recovery

`list-restore-points` walks every host directory under `BACKUP_PATH`, not just the current `HOST_ID`, so a recovery host that mounted the backup tree can see and restore every host's backups. `restore` follows the same path: it picks up the chain from whichever host directory contains a matching `(uuid, timestamp)`. When that host does not match the local one (or no local VM with that UUID exists), the turnkey define path runs.

### How chains map to snapshots

Backups live under:

```
BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/
```

`<chain-id>` is the UTC timestamp of the first run that opened the chain (e.g. `20260501T023000`). Inside the chain dir, `virtnbdbackup` writes:

- One `*-full-*.data` from the first run that opened the chain (`-l full`).
- Subsequent runs the same month append `*-inc-*.data` snapshots and `<checkpoint>.checkpoint` files to the **same** chain dir (`-l inc`); the dir name does not change.
- `metadata.json` describing the disks and checkpoints (written by virtnbdbackup).
- `runs.jsonl` — one JSON line per successful run (`{"ts": "<YYYYMMDDTHHMMSS>", "checkpoint": "<name>"}`). `list-restore-points` reads it to emit one copy-paste row per run; `restore` looks up the operator-supplied timestamp here to derive the `virtnbdrestore --until` checkpoint.

A new chain (a new full) is started when any of these happen:

- A new calendar month begins (the run lands under a fresh `<yyyy-mm>/`).
- The VM's libvirt XML fingerprint changes (e.g. disk added).
- The previous chain dir was deleted out-of-band.

`restore` selects the chain from whichever host directory under `BACKUP_PATH` contains a run record (or, for legacy chains, a chain dir) whose timestamp equals the argument. Inside the chain it picks the recorded checkpoint and passes `virtnbdrestore -a restore -i <chain-dir> -o <output-dir> --until <checkpoint> --name <vm-name> -D`, so the replay stops exactly at the requested run and libvirt is left with a redefined domain pointing at the restored disks. Legacy chains without `runs.jsonl` omit `--until` and replay end-to-end.

The restore command holds the same run-lock as `run` to avoid reading a chain dir that a concurrent backup is still writing into. See [Manual restore process](manual-restore.md) for the lower-level recovery procedure (useful when the source backup must first be staged onto local storage).

## Retention

Old month directories are pruned automatically at the end of every successful `run` when `BACKUP_CLEANUP_ON_RUN=true` (the default). The number of most-recent calendar months to keep is `BACKUP_RETENTION_MONTHS` (default `12`, roughly one year); `0` disables pruning entirely. Pruning is per-VM and only touches `<vm-uuid>/<yyyy-mm>/` directories — foreign files dropped under a VM dir are left alone. The most recent month dir is always preserved even if retention math would otherwise drop it.
