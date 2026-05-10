# Command reference

## `install`

Installs the package copy, wrapper script, config file, and systemd units when `BACKUP_PATH` is configured.

```sh
sudo libvirt-backup-system install
```

## `uninstall`

Removes installed program files and systemd units. Config, state, logs, and backups are preserved unless purge flags are passed.

```sh
sudo libvirt-backup-system uninstall
sudo libvirt-backup-system uninstall --purge-config --purge-state --purge-logs
```

`--purge-backups` removes the configured host backup tree after safety checks.

## `check` / `preflight`

Validates config, required binaries, root execution policy, VM discovery, backup path writability, and estimated free space.

```sh
sudo libvirt-backup-system check
```

## `run`

Runs preflight, acquires the run lock, backs up selected VMs, and applies retention cleanup.

```sh
sudo libvirt-backup-system run
```

## `list-vms`

Lists selected VMs after applying `VM_BLACKLIST`.

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system list-vms --json
sudo libvirt-backup-system list-vms --include-blacklisted
```

## `verify`

Runs `virtnbdrestore -o verify` against discovered backup directories.

```sh
sudo libvirt-backup-system verify
sudo libvirt-backup-system verify --vm my-vm
```

## `cleanup`

Prunes old monthly backup directories according to `BACKUP_RETENTION_MONTHS`.

```sh
sudo libvirt-backup-system cleanup
```

## Restore

There is no restore command. Restoring is intentionally manual; see [Manual restore process](manual-restore.md).
