# libvirt-backup-system

Python CLI for backing up libvirt VMs into a per-host [Kopia](https://kopia.io)
repository. Content-addressed, deduplicated, encrypted at rest. One shared
token protects every host's repo, so cross-host restore is a single command.

## One-command install

From a checkout on the first KVM host, run install with the shared
`BACKUP_PATH`. When no Kopia password flag is supplied and no password file
exists yet, `install` generates a shared token, stores it securely, and uses it
to create this host's repo:

```sh
sudo env BACKUP_PATH=/home/admin/pro/vms/backups python3 -m libvirt_backup_system install
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

`install` writes the token to `/etc/libvirt-backup-system/kopia.pw` mode 600
and creates this host's repo at `BACKUP_PATH/<host-id>/kopia-repo/`. `start`
installs or refreshes the systemd units from the environment file and activates
the backup, maintenance, full-maintenance, and verify schedules. The default
backup schedule is `*-*-* 02:30:00`.

If you installed before setting `BACKUP_PATH`, edit the environment file and
run `sudo libvirt-backup-system start` once before `check`; `check` expects the
local Kopia repo to already exist.

The shared token is the only thing protecting your backups. Save it in your
password manager after first install:

```sh
sudo libvirt-backup-system show-token
```

To join another KVM host to the same backup set, run this on an installed host:

```sh
sudo libvirt-backup-system add-node
```

It prints a pasteable `sudo env BACKUP_PATH=... KOPIA_PW=... python3 -m
libvirt_backup_system install --kopia-password-env KOPIA_PW
--acknowledge-password-loss` command for the new host. See [Joining
hosts](docs/joining-hosts.md).

## Basic use

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system verify
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
sudo libvirt-backup-system change-password --new-kopia-password=<value>
```

Backups are normally taken by the installed systemd timer. To trigger one
manually, `run` starts the backup in the background via systemd and returns
immediately — it keeps running to completion even if you log out — and `log`
shows its journal, with `-f` to stream live like `docker logs -f`:

```sh
sudo libvirt-backup-system run        # starts in the background
sudo libvirt-backup-system log -f     # follow the running backup
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
New restore points also record their backup consistency level; see
[Backup consistency](docs/backup-consistency.md) for QEMU guest agent setup
and the crash/filesystem distinction.

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
- [Joining additional hosts](docs/joining-hosts.md)
- [Backup consistency and QEMU guest agent setup](docs/backup-consistency.md)
- [Configuration reference](docs/env-vars.md)
- [Command reference](docs/commands.md)
- [Kopia repo layout, manual operations](docs/kopia.md)
- [Kopia password handling, rotation, and recovery](docs/kopia-password.md)
- [Manual restore process](docs/manual-restore.md)
- [Testing on Linux](docs/testing-on-linux.md)
