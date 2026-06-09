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
| `kind=meta` | `vm-uuid`, `vm-name`, `timestamp`, `run-id`, `host`, `consistency` | `manifest.json` |

The `manifest.json` carries the VM name, UUID, host id, run id, timestamp,
libvirt URI, full domain XML, and the per-disk table (target name, source
path, virtual size, snapshot filename). New manifests also carry
`consistency`, which is `filesystem`, `crash`, or `unknown` for older restore
points. See [Backup consistency](backup-consistency.md) for the QEMU guest
agent behavior behind that value. `restore` joins disk + meta snapshots by
`run-id`.

The kopia source identifier is overridden per snapshot to
`root@<host-id>:libvirt-backup:<vm-uuid>/<target>` for disks and
`root@<host-id>:libvirt-backup:<vm-uuid>/meta` for meta, matching Kopia's
`username@host:path` source parser so `kopia snapshot list` groups runs by
VM under each host.

## Password handling

The shared token lives in `$KOPIA_PASSWORD_FILE` (default
`/etc/libvirt-backup-system/kopia.pw`, mode 600 root-owned). The same value
exists on every participating host. The wrapper reads it via
`KOPIA_PASSWORD` env-var (not `--password-file`) so the file path never
appears in `ps` or journald.

See [kopia-password.md](kopia-password.md) for install-time write, rotation,
joining additional hosts, half-rotation recovery, single-host password loss,
and total-loss scenarios.

## Multi-host cutover

When migrating an existing fleet from the previous (non-kopia) install:

1. On every host: stop the existing systemd timer.
2. On every host: delete (or move aside) the pre-kopia chain trees under
   `BACKUP_PATH/<host>/<vm-uuid>/<yyyy-mm>/...`. These are leftover artifacts
   from the previous (non-kopia) install and are not read by the new code;
   leaving them in place only wastes space on the backup share. See
   "Removing pre-kopia chain backups" below for the safe listing / delete
   commands.
3. On the first host, install with the shared backup path:
   ```sh
   sudo env BACKUP_PATH=/mnt/qnap-backups libvirt-backup-system install
   ```
   This generates the shared token, creates the first local repo at
   `BACKUP_PATH/$HOST_ID/kopia-repo/`, applies global policy, and registers
   timers. Save the token with `sudo libvirt-backup-system show-token`.
4. On the first host, run `sudo libvirt-backup-system add-node` and paste the
   printed command on each additional host.
5. On every host: `sudo libvirt-backup-system check` must pass clean.
6. On every host: `sudo libvirt-backup-system start` enables timers.
7. On every host: `sudo libvirt-backup-system doctor` must pass clean. The
   peer-repo connect smoke test confirms the shared token reaches every host's
   repo.
8. The first scheduled run on each host is a full backup into its own
   fresh repo. Subsequent runs are deduplicated against the existing
   chunks.

### Removing pre-kopia chain backups

The old chain layout was
`$BACKUP_PATH/<host>/<vm-uuid>/<yyyy-mm>/<chain>/`. The new kopia repo lives
beside it at `$BACKUP_PATH/<host>/kopia-repo/` — do NOT delete that
directory. List the legacy trees first, confirm the listing matches your
expectation, then delete:

```sh
# dry-run: enumerate legacy chain dirs without touching them
sudo find "$BACKUP_PATH" -mindepth 3 -maxdepth 3 -type d \
     -not -path "*/kopia-repo/*" -not -name kopia-repo

# delete after the listing looks right
sudo find "$BACKUP_PATH" -mindepth 2 -maxdepth 2 -type d \
     -not -name kopia-repo -exec rm -rf {} +
```

The `kopia-repo` directories survive both commands because the prune
clauses exclude them by name.

## Manual kopia operations

The wrapper drops a per-repo config file for every connection. Reuse the
local one for ad-hoc inspection:

```sh
HOST_ID=$(cat /etc/machine-id)
CFG=/var/lib/libvirt-backup-system/kopia-configs/${HOST_ID}.config
PW=/etc/libvirt-backup-system/kopia.pw
export KOPIA_PASSWORD="$(sudo cat "$PW")"

sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" repository status
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" snapshot list --all
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" policy show --global
```

(Substitute the file under `kopia-configs/` matching your configured
`HOST_ID` if you changed it from the default machine-id value.)

For a peer host's repo:

```sh
CFG=/var/lib/libvirt-backup-system/kopia-configs/host-a.config
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" snapshot list --all --tags=vm-uuid:<uuid>
```

`doctor` lays these per-peer configs down as a side effect of its smoke
test, so this works out of the box on a healthy host.

The CLI also has a hidden helper for the same ad-hoc work:

```sh
sudo libvirt-backup-system kopia-passthrough -- snapshot list --all
sudo libvirt-backup-system kopia-passthrough --host-id=host-a -- snapshot list --tags=vm-uuid:<uuid>
```

It resolves the managed `--config-file` and password environment before
execing `kopia`. Keep the literal `--` between wrapper options and Kopia
arguments so Kopia flags are forwarded unchanged.

## Maintenance

Each host maintains its own repo via a per-host systemd timer
(`libvirt-backup-system-maintenance.timer`, default interval
`KOPIA_MAINTENANCE_INTERVAL=24h`). No global owner, no cross-host
coordination. Daily quick maintenance, weekly full maintenance.
Maintenance and verify timers use activation-relative initial delays, then
continue on their configured `OnUnitActiveSec` cadence.

Manual run:

```sh
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" maintenance run --safety=full
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" maintenance run --full --safety=full
```

Quick maintenance compacts indexes and runs short-running cleanups; full
maintenance runs garbage collection on unreachable chunks. Setup does not
claim an explicit Kopia maintenance owner; each per-host repo remains
independent and no cross-host owner coordination is required.

## Verify

The verify timer (`libvirt-backup-system-verify.timer`,
`KOPIA_VERIFY_INTERVAL=7d`) runs `libvirt-backup-system verify`, which takes
the same run lock as manual verification and checks 1% of files in the local
repo. Cross-host verify is opt-in:

```sh
sudo libvirt-backup-system verify
sudo libvirt-backup-system verify --include-hosts=host-a,host-b
```

Manual verify of a single snapshot:

```sh
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" snapshot verify --max-errors=0 <snap-id>
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
server` and uses standard `kopia repository sync-to <provider>`.
libvirt-backup-system does not run, supervise, or configure `kopia server`
— it is an external piece of infrastructure the operator owns separately
(systemd unit, TLS, auth). Mentioned here only because operators
replicating offsite sometimes pair the two.

**Direct repo-to-repo sync.** On a host with both repos connected:

```sh
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" repository sync-to filesystem --path /mnt/offsite/host-a/kopia-repo --must-exist
```

Or via rclone for cloud targets:

```sh
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" repository sync-to rclone --remote-path s3:my-bucket/host-a-repo
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
sudo env KOPIA_PASSWORD="$KOPIA_PASSWORD" kopia --config-file="$CFG" maintenance run --full --safety=full
```

The local maintenance units run against this host's own repo. Setup does not
claim an explicit Kopia maintenance owner; each per-host repo remains
independent and no cross-host owner coordination is required.
