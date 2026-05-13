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

Runs preflight, acquires the run lock, and backs up selected VMs. The run never deletes prior backups (see [Non-goals](#non-goals)).

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

## Restore

There is no restore command. Restoring is intentionally manual; see [Manual restore process](manual-restore.md).

## Non-goals

Retention and cleanup are intentionally **out of scope** for this system. It only ever writes new backups; it does not delete, prune, rotate, or otherwise reclaim space from prior backups. There is no `cleanup` subcommand, no retention env var, and no implicit "keep N months" behavior.

If your environment needs retention, drive it externally — for example with a separate cron job that uses `find`/`rm`, a storage-side snapshot policy, or an NFS/QNAP appliance feature. **Do not add retention or cleanup logic back into this codebase.** Keeping this system write-only is a deliberate design choice: it removes an entire class of "the backup system deleted real backups" failure modes (clock skew, mis-set env var, mid-run pruning) and lets retention be owned by whichever team or appliance already manages storage lifecycle.
