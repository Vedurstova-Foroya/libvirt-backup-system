# libvirt-backup-system

Python CLI for backing up libvirt VMs into a per-host [Kopia](https://kopia.io)
repository. Content-addressed, deduplicated, encrypted at rest. One shared
password protects every host's repo, so cross-host restore is a single command.

## One-command install

From a checkout on the KVM host, pick a shared password once, store it in your
secrets vault, and use the same value on every host:

```sh
export KOPIA_PW='<shared-password-from-vault>'; sudo env BACKUP_PATH=/home/admin/pro/vms/backups KOPIA_PW="$KOPIA_PW" python3 -m libvirt_backup_system install --kopia-password-env KOPIA_PW --acknowledge-password-loss
```

Local backup directories are allowed by default. To require `BACKUP_PATH` to be
a mounted filesystem, set `BACKUP_REQUIRE_NFS_MOUNT=true` in
`/etc/libvirt-backup-system/libvirt-backup.env`.

Then verify the install, activate the schedules, and run a health check:

```sh
sudo libvirt-backup-system check
sudo libvirt-backup-system start
sudo libvirt-backup-system doctor
```

`install` writes the password to `/etc/libvirt-backup-system/kopia.pw` mode 600
and creates this host's repo at `BACKUP_PATH/<host-id>/kopia-repo/`. `start`
installs or refreshes the systemd units from the environment file and activates
the backup, maintenance, full-maintenance, and verify schedules. The default
backup schedule is `*-*-* 02:30:00`.

The shared password is the only thing protecting your backups. Lose it on
every host and the data is unrecoverable. Store a copy in your secrets vault.

## Basic use

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system verify
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
sudo libvirt-backup-system change-password --new-kopia-password=<value>
```

`list-restore-points` lists every restorable snapshot across all hosts. The
`vm-uuid` and `timestamp` columns are the values to copy into `restore`.
`restore` either overwrites the local VM (when the snapshot came from this
host and the domain exists locally) or stages and defines the VM turnkey from
the backup (everywhere else). No `--source-host` flag — cross-host restore is
the same command as same-host restore.

## Backup layout

Only running VMs are backed up. Offline VMs are logged as `skipping vm because
it is offline` and skipped — bring the VM up to back it up.

Each host writes only to its own repo:

```
BACKUP_PATH/
  <host-a-id>/kopia-repo/
  <host-b-id>/kopia-repo/
  <host-c-id>/kopia-repo/
```

Per host, per VM, per backup run the orchestrator creates one Kopia snapshot
per disk plus one meta snapshot carrying the run manifest (domain XML, disk
table, run id). Snapshots are tagged with `vm-uuid`, `run-id`, `disk`, `host`,
and `kind`; meta snapshots also carry `vm-name` and `timestamp` so
restore-point listings can show the domain name and exact run timestamp
without materializing the manifest.

## Retention

Retention is enforced by Kopia's global policy, refreshed from the env file on
every `start`. Defaults keep the latest 8 snapshots plus hourly points for 24h
and daily points for one year: `KEEP_LATEST=8`, `KEEP_HOURLY=24`,
`KEEP_DAILY=365`, `KEEP_WEEKLY=0`, `KEEP_MONTHLY=0`, `KEEP_ANNUAL=0`. The maintenance timer
(`KOPIA_MAINTENANCE_INTERVAL`, default `24h`) prunes and compacts the repo
independently of the backup loop. The verify timer (`KOPIA_VERIFY_INTERVAL`,
default `7d`) checks the local repo on its own cadence.

## Docs

- [Install and prerequisites](docs/install.md)
- [Configuration reference](docs/env-vars.md)
- [Command reference](docs/commands.md)
- [Kopia repo layout, manual operations](docs/kopia.md)
- [Kopia password handling, rotation, and recovery](docs/kopia-password.md)
- [Manual restore process](docs/manual-restore.md)
- [Testing on Linux](docs/testing-on-linux.md)
