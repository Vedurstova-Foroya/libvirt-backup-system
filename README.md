# libvirt-backup-system

Python CLI for backing up libvirt VMs with `virtnbdbackup` into a configured monthly backup tree.

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
sudo libvirt-backup-system cleanup
```

## Docs

- [Install and prerequisites](docs/install.md)
- [Configuration reference](docs/env-vars.md)
- [Command reference](docs/commands.md)
- [Manual restore process](docs/manual-restore.md)
- [Testing on Linux](docs/testing-on-linux.md)
