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

## Wrong token behavior

If peer repos already exist under `BACKUP_PATH` and the new host is installed
with the wrong token, install fails because it cannot decrypt the existing
repos. Fix the token and rerun the printed `add-node` command; do not rotate
tokens unless you intentionally want to change the shared token for every
host.
