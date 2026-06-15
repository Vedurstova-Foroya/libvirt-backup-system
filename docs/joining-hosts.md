# Joining additional hosts

Every host writes to its own Kopia repo under the same `BACKUP_PATH`, and all
repos share one token. The first host can generate that token automatically;
additional hosts should join with the exact same value so `list-restore-points`
and `restore` can read every peer repo.

## First host

Run install with the shared backup path:

```sh
sudo env BACKUP_PATH=/home/admin/pro/vms/backups libvirt-backup-system install
sudo libvirt-backup-system check
sudo libvirt-backup-system start
```

When no password file exists and no `--kopia-password*` flag is supplied,
`install` generates the shared token and stores it in
`/etc/libvirt-backup-system/kopia.pw` mode 600 root-owned.

Save the token in a password manager:

```sh
sudo libvirt-backup-system show-token
```

`show-token` prints the raw secret. Avoid leaving it in logs or shell history.

## New host

On an already installed host, print the join command:

```sh
sudo libvirt-backup-system add-node
```

The output is a pasteable command in this shape:

```sh
sudo env BACKUP_PATH=... KOPIA_PW=... python3 -m libvirt_backup_system install --kopia-password-env KOPIA_PW --acknowledge-password-loss
```

Run that command on the new KVM host from a checkout of this project. It uses
the same shared token but creates a separate repo for the new host:

```text
BACKUP_PATH/
  <existing-host-id>/kopia-repo/
  <new-host-id>/kopia-repo/
```

Then activate and validate schedules on the new host:

```sh
sudo libvirt-backup-system start
sudo libvirt-backup-system check
sudo libvirt-backup-system doctor
```

If `check` or `start` says the host is not joined or cannot open an existing
peer repo, run `sudo libvirt-backup-system add-node` on an already joined host
and paste the printed install command on this host.

## Shared configuration

The env file is shared across hosts through the backup tree so a new host
inherits the cluster's settings instead of starting from defaults. A single
seed file lives next to the per-host repos:

```text
BACKUP_PATH/
  libvirt-backup.env          # shared config seed
  <existing-host-id>/kopia-repo/
  <new-host-id>/kopia-repo/
```

The seed is a **template, not a live-synced file**:

- The first host publishes it automatically — during `install` when
  `BACKUP_PATH` is set, and on `start`. It is written only when no seed exists
  yet, so it is never silently overwritten.
- A joining host pulls the seed as its initial local config, inheriting
  retention, splitter, compression, NFS policy, and the backup schedule
  without re-typing them. The host's own install-time `BACKUP_PATH` still wins
  over the seed's recorded value.
- After joining, the local config is **independent**. Edit
  `/etc/libvirt-backup-system/libvirt-backup.env` and run `start` to change
  only that host (its own backup timer, mount path, etc.) — the seed is not
  touched.

`HOST_ID` is never shared: it scopes the per-host repo
(`BACKUP_PATH/<HOST_ID>/kopia-repo/`), so each node keeps its own (falling
back to `/etc/machine-id`).

### Updating the shared config

To make a host's current config the template that **future** joins inherit,
publish it explicitly:

```sh
sudoedit /etc/libvirt-backup-system/libvirt-backup.env
sudo libvirt-backup-system start          # apply locally
sudo libvirt-backup-system update-config  # publish for future joins
```

`update-config` overwrites the seed (last writer wins). It only affects hosts
that join *after* it runs; already-joined hosts keep their independent config.

## Wrong token behavior

If peer repos already exist under `BACKUP_PATH` and the new host is installed
with the wrong token, install fails because it cannot decrypt the existing
repos. Fix the token and rerun the printed `add-node` command; do not rotate
tokens unless you intentionally want to change the shared token for every
host.
