# Kopia operations

This page documents the on-disk layout, password handling, manual kopia
operations, maintenance, and remote-repo sync paths. The wrapper subcommands
(see [commands.md](commands.md)) handle the day-to-day cases; reach for the
raw `kopia` CLI when you need something the wrapper does not cover.

## Repo layout

```
BACKUP_PATH/
  <host-a-id>/kopia-repo/
    kopia.repository.f       # repo sentinel; presence means "repo exists"
    _log_<...>               # operation logs (kopia-internal)
    _v<n>_                   # format / version metadata
    indexes/                 # snapshot indexes
    p<...>/                  # content-addressed encrypted chunks
  <host-b-id>/kopia-repo/
    ...
```

One repo per host, identified by its `HOST_ID` directory. Each host writes
only to its own repo. Cross-host operations read peer repos read-only, using
the shared password.

Local per-repo connection configs live at:

```
/var/lib/libvirt-backup-system/kopia-configs/<host-id>.config
```

Kopia's default single-config-file layout cannot hold multiple repo
connections at once; the wrapper allocates one file per repo it touches
(local + each peer).

The chunk cache (rebuilt on demand from the repo, safe to delete) lives at:

```
/var/cache/libvirt-backup-system/kopia/
```

## Tag schema

Each backup run produces two kinds of snapshots:

| Kind | Tags | Content |
|---|---|---|
| `kind=disk` | `vm-uuid`, `run-id`, `disk=<target>`, `host` | One logical file `<target>.raw` |
| `kind=meta` | `vm-uuid`, `run-id`, `host` | `manifest.json` |

The `manifest.json` carries the VM name, UUID, host id, run id, timestamp,
libvirt URI, full domain XML, and the per-disk table (target name, source
path, virtual size, snapshot filename). `restore` joins disk + meta
snapshots by `run-id`.

The kopia source identifier is overridden per snapshot to
`<host-id>:libvirt-backup:<vm-uuid>/<target>` for disks and
`<host-id>:libvirt-backup:<vm-uuid>/meta` for meta, so `kopia snapshot list`
groups runs by VM under each host.

## Password handling

The shared password file lives at `$KOPIA_PASSWORD_FILE` (default
`/etc/libvirt-backup-system/kopia.pw`, mode 600 root-owned). The same value
exists on every participating host. The wrapper reads it via
`KOPIA_PASSWORD` env-var (not `--password-file`) so the file path never
appears in `ps` or journald.

### Install-time write

`install --kopia-password*` resolves the value from one of:

```sh
--kopia-password=VALUE              # literal (visible in ps/journald)
--kopia-password-file=/path         # path on disk
--kopia-password-file=-             # stdin (preferred for config-management)
--kopia-password-env=VARNAME        # named env var
```

Atomic write + chmod 600 + chown root. Idempotent if the file already
matches; hard-fails on a mismatch.

### Password rotation

```sh
sudo libvirt-backup-system change-password --new-kopia-password=<value>
```

Per host:

1. Validate the current password file decrypts the local repo.
2. `kopia repository change-password` rewraps the master key.
3. Atomically replace the password file with the new value.

Run the same command on every host with the same new value. Order does not
matter — each host rotates its local repo independently. `doctor` flags
hosts that are out of step (local repo decrypts with the file's password but
a peer's does not), so partial rotations are visible.

### Recovery from a half-rotated host

If step 3 fails after step 2 succeeds (full disk, etc), the repo decrypts
only with the new value but the file still holds the old one. The log line
prints both placeholders. Recover by hand:

```sh
sudo install -m 600 -o root -g root /dev/null /etc/libvirt-backup-system/kopia.pw
echo -n "<new-password>" | sudo tee /etc/libvirt-backup-system/kopia.pw > /dev/null
sudo libvirt-backup-system doctor
```

### Total-loss scenario

If the password is lost on every host, the backups become unreadable. Kopia
does not have a backdoor. Keep an offsite copy in a secrets vault.

## Multi-host cutover

When migrating an existing fleet from the previous (non-kopia) install:

1. Pick a shared password: `openssl rand -base64 32`. Store in your secrets
   vault.
2. On every host: stop the existing systemd timer.
3. On every host: delete (or move aside) the pre-kopia chain trees under
   `BACKUP_PATH/<host>/<vm-uuid>/<yyyy-mm>/...`. These are leftover artifacts
   from the previous (non-kopia) install and are not read by the new code;
   leaving them in place only wastes space on the backup share.
4. On every host (identical command line):
   ```sh
   sudo libvirt-backup-system install --kopia-password=<value>
   ```
   Creates the local repo at `BACKUP_PATH/$HOST_ID/kopia-repo/`, applies
   global policy, registers timers.
5. On every host: `sudo libvirt-backup-system doctor` must pass clean. The
   peer-repo connect smoke test confirms the shared password reaches every
   host's repo.
6. On every host: `sudo libvirt-backup-system start` enables timers.
7. The first scheduled run on each host is a full backup into its own
   fresh repo. Subsequent runs are deduplicated against the existing
   chunks.

## Manual kopia operations

The wrapper drops a per-repo config file for every connection. Reuse the
local one for ad-hoc inspection:

```sh
CFG=/var/lib/libvirt-backup-system/kopia-configs/$(hostname).config
PW=/etc/libvirt-backup-system/kopia.pw
export KOPIA_PASSWORD="$(sudo cat "$PW")"

sudo -E kopia --config-file="$CFG" repository status
sudo -E kopia --config-file="$CFG" snapshot list --all
sudo -E kopia --config-file="$CFG" policy show --global
```

(Substitute the file under `kopia-configs/` matching your `HOST_ID` if it
differs from `hostname`.)

For a peer host's repo:

```sh
CFG=/var/lib/libvirt-backup-system/kopia-configs/host-a.config
sudo -E kopia --config-file="$CFG" snapshot list --all --tags=vm-uuid:<uuid>
```

`doctor` lays these per-peer configs down as a side effect of its smoke
test, so this works out of the box on a healthy host.

## Maintenance

Each host maintains its own repo via a per-host systemd timer
(`libvirt-backup-system-maintenance.timer`, default interval
`KOPIA_MAINTENANCE_INTERVAL=24h`). No global owner, no cross-host
coordination. Daily quick maintenance, weekly full maintenance.

Manual run:

```sh
sudo -E kopia --config-file="$CFG" maintenance run
sudo -E kopia --config-file="$CFG" maintenance run --full
```

Quick maintenance compacts indexes and runs short-running cleanups; full
maintenance runs garbage collection on unreachable chunks. The maintenance
owner is set to `$HOST_ID@$HOST_ID` at install time so kopia's owner
heuristics don't pick a wrong host.

## Verify

The verify timer (`libvirt-backup-system-verify.timer`,
`KOPIA_VERIFY_INTERVAL=7d`) runs `kopia snapshot verify --max-failures=0`
against the local repo. Cross-host verify is opt-in:

```sh
sudo libvirt-backup-system verify
sudo libvirt-backup-system verify --include-hosts=host-a,host-b
```

Manual verify of a single snapshot:

```sh
sudo -E kopia --config-file="$CFG" snapshot verify --max-failures=0 <snap-id>
```

## Disaster recovery: lost host

If a host dies, its repo lives on under `BACKUP_PATH/<dead-host-id>/kopia-
repo/`. Any surviving host can restore VMs from it with the standard
`restore` command — the shared password decrypts every host's repo, and
peer-repo discovery picks up the dead host automatically. Delete the dead
host's repo directory when no longer needed:

```sh
sudo rm -rf /mnt/qnap-backups/<dead-host-id>/kopia-repo
```

## Sync to a remote repo

Kopia supports replicating a filesystem repo to a remote location (S3,
GCS, Azure, Backblaze B2, WebDAV, SFTP, rclone). The wrapper does not
manage this; configure it directly with kopia. Two common patterns:

**Server-side `kopia server` + remote pull.** Run `kopia server` on the
NFS host; the offsite worker connects with `kopia repository connect
server` and uses standard `kopia snapshot sync-to <remote>`.
libvirt-backup-system does not run, supervise, or configure `kopia server`
— it is an external piece of infrastructure the operator owns separately
(systemd unit, TLS, auth). Mentioned here only because operators
replicating offsite sometimes pair the two.

**Direct repo-to-repo sync.** On a host with both repos connected:

```sh
sudo -E kopia --config-file="$CFG" snapshot sync-to filesystem --path /mnt/offsite/host-a/kopia-repo
```

Or via rclone for cloud targets:

```sh
sudo -E kopia --config-file="$CFG" repository sync-to rclone --remote-path s3:my-bucket/host-a-repo
```

The sync runs while backups continue on the primary; kopia's filesystem
repo tolerates readers during writes. Run sync on a separate schedule (not
inline with backups) to avoid IO contention. See the [Kopia docs on
syncing](https://kopia.io/docs/repository/#syncing-a-repository) for the
full option set.

## Garbage collection

GC is part of `kopia maintenance run --full` (weekly by default). It walks
the repo's snapshot index, marks every chunk reachable from any live
snapshot, and deletes unreferenced chunks. Retention (which deletes
snapshots) is separate and happens automatically per the global policy.

To force a GC pass after a manual retention adjustment:

```sh
sudo -E kopia --config-file="$CFG" maintenance run --full
```

If maintenance is reporting that another owner holds the lock, claim it
explicitly:

```sh
sudo -E kopia --config-file="$CFG" maintenance set --owner="$(hostname)@$(hostname)"
```

Be careful: only one host should own maintenance for a given repo. In our
layout each host owns its own repo, so the owner is always set to the local
host id at install time.
