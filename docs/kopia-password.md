# Kopia password handling

This page covers password lifecycle (install, rotation, recovery) for the
kopia engine. The day-to-day commands live in [commands.md](commands.md); the
on-disk layout and manual operations live in [kopia.md](kopia.md).

The shared password file lives at `$KOPIA_PASSWORD_FILE` (default
`/etc/libvirt-backup-system/kopia.pw`, mode 600 root-owned). The same value
exists on every participating host. The wrapper reads it via
`KOPIA_PASSWORD` env-var (not `--password-file`) so the file path never
appears in `ps` or journald.

## Install-time write

`install --kopia-password*` resolves the value from one of:

```sh
--kopia-password=VALUE              # literal (visible in ps/journald)
--kopia-password-file=/path         # path on disk
--kopia-password-file=-             # stdin (preferred for config-management)
--kopia-password-env=VARNAME        # named env var
--acknowledge-password-loss         # required when writing the password first time
```

Atomic write + chmod 600 + chown root. Idempotent if the file already
matches; hard-fails on a mismatch.

## Password rotation

```sh
sudo libvirt-backup-system change-password --new-kopia-password=<value>
sudo libvirt-backup-system change-password --new-kopia-password-file=/path
echo -n "$PW" | sudo libvirt-backup-system change-password --new-kopia-password-file=-
sudo env NEW_KOPIA_PW=... libvirt-backup-system change-password --new-kopia-password-env=NEW_KOPIA_PW
```

Per host:

1. Validate the current password file decrypts the local repo.
2. `kopia repository change-password` rewraps the master key.
3. Atomically replace the password file with the new value.

Kopia currently documents `--new-password` as the noninteractive input for
`repository change-password`; it does not document a `--new-password-file`
or stdin equivalent. The wrapper therefore keeps its own CLI safer for
operators by accepting `--new-kopia-password-file=-` and env/file sources,
but the final Kopia subprocess still receives the resolved new password in
its argv. Avoid running rotation on hosts where untrusted users can inspect
process arguments.

Run the same command on every host with the same new value. Order does not
matter — each host rotates its local repo independently. `doctor` flags
hosts that are out of step (local repo decrypts with the file's password but
a peer's does not), so partial rotations are visible.

## Recovery from a half-rotated host

If step 3 fails after step 2 succeeds (full disk, etc), the repo decrypts
only with the new value but the file still holds the old one. The emergency
recovery log includes the actual old and new password values; treat that log
line as a secret. Recover by hand:

```sh
sudo install -m 600 -o root -g root /dev/null /etc/libvirt-backup-system/kopia.pw
echo -n "<new-password>" | sudo tee /etc/libvirt-backup-system/kopia.pw > /dev/null
sudo libvirt-backup-system doctor
```

## Recovering when the rotation log is lost

The recovery log line above is the only in-band record of the new value when
step 3 fails. If it is lost (journald rotation, ssh disconnect, closed
terminal), Kopia itself has no backdoor — once `kopia repository
change-password` succeeds, the repo's master key is wrapped only under the
new password.

Diagnose which side is out of sync by attempting a read-only connect with
whatever the file currently holds:

```sh
HOST_ID=$(cat /etc/machine-id)
CFG=/tmp/lbs-probe.config
sudo env KOPIA_PASSWORD="$(sudo cat /etc/libvirt-backup-system/kopia.pw)" \
     kopia --config-file="$CFG" repository connect filesystem \
     --path "$BACKUP_PATH/$HOST_ID/kopia-repo" --readonly
```

If the connect succeeds, the file already matches the repo (rotation did
not actually complete on this host — re-run `change-password`). If it
fails with a decryption error, the file holds the old password and the
repo wants the new one; the operator must retrieve the new value from
their secrets vault, paste it into `$KOPIA_PASSWORD_FILE` with the
mode-600 helper above, and re-run `doctor` to confirm.

## If only one host loses its password file

A surviving host carries the same value (the shared-password convention).
Copy the value out of a healthy host's `$KOPIA_PASSWORD_FILE` (over SSH or
via your secrets vault) and recreate the file on the bare host:

```sh
# on the healthy host:
sudo cat /etc/libvirt-backup-system/kopia.pw

# on the host missing its password file, with the same value:
sudo libvirt-backup-system install --kopia-password=<value> --acknowledge-password-loss
```

The repo on the bare host is unchanged; install only rewrites the password
file and reconnects. After it returns, `doctor` should pass clean.

## Total-loss scenario

If the password is lost on every host, the backups become unreadable. Kopia
does not have a backdoor. Keep an offsite copy in a secrets vault.
