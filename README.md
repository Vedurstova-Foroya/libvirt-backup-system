# libvirt-backup-system

Python CLI for backing up libvirt VMs with `virtnbdbackup` into a configured monthly incremental backup tree.

## One-command install

From a checkout on the KVM host, set the backup mount path and install:

```sh
sudo env BACKUP_PATH=/mnt/qnap-backups python3 -m libvirt_backup_system install
```

Then verify and run once:

```sh
sudo libvirt-backup-system check
sudo libvirt-backup-system run
```

The systemd timer is installed with the default schedule `*-*-* 02:30:00`.

## Basic use

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system verify
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm
```

## Backup layout

Backups live under `BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/`. Running VMs build a per-month incremental chain: the first run each calendar month writes a full into a new chain directory, later runs in the same month add `-l inc` snapshots to the same chain. Inactive (shut-off) VMs keep their `-l copy` semantics with a per-month `.inactive-copy-complete` marker.

## Retention

Old month directories are pruned automatically after every successful `run`. The default `BACKUP_RETENTION_MONTHS=12` keeps roughly one year; set it to `0` to disable pruning entirely. Set `BACKUP_CLEANUP_ON_RUN=false` to keep retention manual without changing the keep window. The most recent month dir is always preserved, even if retention math would otherwise drop it.

## Docs

- [Install and prerequisites](docs/install.md)
- [Configuration reference](docs/env-vars.md)
- [Command reference](docs/commands.md)
- [Manual restore process](docs/manual-restore.md)
- [Testing on Linux](docs/testing-on-linux.md)
