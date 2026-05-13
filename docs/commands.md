# Command reference

## `install`

Installs the package copy, wrapper script, config file, and systemd units when `BACKUP_PATH` is configured.

```sh
sudo libvirt-backup-system install
```

When run via the installed wrapper the package files at `/opt/libvirt-backup-system` are kept as-is (refreshing them mid-execute would delete the source being copied). The wrapper, env file, and systemd units are still re-rendered, so this form is the right one for changing `BACKUP_PATH` or `SYSTEMD_ON_CALENDAR`. To pick up new package code, run `install` from a source checkout instead:

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

Reconstructs a VM into an empty staging directory using `virtnbdrestore -a restore`. Defaults to the latest month and latest chain for the named VM; pass `--month YYYY-MM` and/or `--chain <chain-id>` to pin a specific snapshot. The output directory must either not exist or be empty — restore refuses to overwrite existing data.

```sh
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm --month 2026-05
sudo libvirt-backup-system restore --vm <uuid> --output /var/tmp/restore/my-vm --month 2026-05 --chain 20260507T101112_000000Z
```

The restore command holds the same run-lock as `run` to avoid reading a chain dir that a concurrent backup is still writing into. See [Manual restore process](manual-restore.md) for the lower-level recovery procedure (useful when the source backup must first be staged onto local storage).

## Retention

Old month directories are pruned automatically at the end of every successful `run` when `BACKUP_CLEANUP_ON_RUN=true` (the default). The number of most-recent calendar months to keep is `BACKUP_RETENTION_MONTHS` (default `12`, roughly one year); `0` disables pruning entirely. Pruning is per-VM and only touches `<vm-uuid>/<yyyy-mm>/` directories — foreign files dropped under a VM dir are left alone. The most recent month dir is always preserved even if retention math would otherwise drop it.
