# Configuration reference

The installed env file lives at
`/etc/libvirt-backup-system/libvirt-backup.env`. Values in the real process
environment override values in this file. Booleans accept (case-insensitive)
`1`, `true`, `yes`, `on` as true; `0`, `false`, `no`, `off` as false. Any
other value is rejected by preflight rather than silently coerced.

## Core

```
LIBVIRT_URI=qemu:///system
```

Libvirt connection used by `virsh` for VM discovery and state checks. This
Kopia engine only supports local libvirt transports (`qemu:///...` or
`qemu+unix://...`) because disk streaming runs local `qemu-nbd` against local
disk paths. Remote transports such as `qemu+ssh://`, `qemu+tcp://`, and
`qemu+tls://` are rejected by preflight.

```
BACKUP_PATH=
```

Root of the shared backup tree. Backups are written to:

```
BACKUP_PATH/<host-id>/kopia-repo/
```

Peer hosts' repos live at sibling `BACKUP_PATH/<other-host-id>/kopia-repo/`
paths. Only running VMs are backed up; offline VMs are logged as `skipping
vm because it is offline` and skipped.

```
HOST_ID=
```

Backup host folder name. Empty means "use this machine's `/etc/machine-id`".
Keep this stable: renaming `HOST_ID` writes new snapshots under a fresh repo
and leaves the old data untouched in the prior `HOST_ID` directory.

```
VM_BLACKLIST=
```

VM UUIDs to skip. Separate with spaces or commas. Use `virsh domuuid
<vm-name>` to look up a VM's UUID. The blacklist scopes to *taking* new
backups; restore and verify ignore it.

```
SYSTEMD_ON_CALENDAR=*-*-* 02:30:00
```

systemd `OnCalendar` value used when the backup timer is installed. Run
`start` after changing this so the backup timer is refreshed and reloaded.

```
BACKUP_REQUIRE_NFS_MOUNT=true
```

Require `BACKUP_PATH` to be a mounted filesystem. Set false when backing up
to an intentionally local directory.

```
REQUIRE_ROOT=true
```

Require preflight and run commands to execute as root.

```
COMMAND_TIMEOUT_SECONDS=86400
```

Timeout for external commands, including backup and restore streaming
pipelines (`qemu-nbd`, `nbdcopy`, `kopia snapshot create/restore`, and
`qemu-img convert`).

## Kopia repo

```
KOPIA_REPO_PATH=
```

Repo path override. Defaults to `BACKUP_PATH/<HOST_ID>/kopia-repo` when
empty. If set, it must still equal that discoverable per-host path; peer
listing and restore intentionally scan only `BACKUP_PATH/*/kopia-repo`.

```
KOPIA_PASSWORD_FILE=/etc/libvirt-backup-system/kopia.pw
```

Path to the shared-password file (mode 600, root-owned). Written by
`install` and rotated by `change-password`. Lose this file on every host
and the repos become unreadable.

```
KOPIA_CACHE_DIR=/var/cache/libvirt-backup-system/kopia
```

Local on-disk cache for Kopia chunk metadata. Speeds up subsequent
operations against the same repo. Can be deleted at any time; Kopia
rebuilds it on demand.

## Kopia tuning

```
KOPIA_PARALLELISM=4
```

Passed to `kopia snapshot create --parallel`. Higher values trade CPU and
read bandwidth for shorter per-VM backup windows; lower values reduce
contention with the running VMs.

```
KOPIA_SPLITTER=FIXED-4M
```

Chunker. Fixed-size is the correct splitter for opaque block streams
(raw disk images coming out of `nbdcopy`). Documented as advanced — change
only with a clean cutover; mixing splitters in one repo defeats dedup.

```
KOPIA_COMPRESSION=zstd-fastest
```

Repo-wide compression. Applied via the global Kopia policy on `start`.

## Retention

Mapped onto `kopia policy set --global --keep-*`. Defaults are tuned for a
single year of hourly granularity:

```
KEEP_LATEST=8
KEEP_HOURLY=24
KEEP_DAILY=30
KEEP_WEEKLY=12
KEEP_MONTHLY=24
KEEP_ANNUAL=5
```

The Kopia maintenance timer (see below) prunes expired snapshots in the
background; the backup loop does not perform pruning itself.

## Maintenance and verify cadence

```
KOPIA_MAINTENANCE_INTERVAL=24h
```

Cadence for `kopia maintenance run` against the local repo. Daily quick
maintenance, weekly full maintenance. No global owner: each host maintains
its own repo.

```
KOPIA_VERIFY_INTERVAL=7d
```

Cadence for `libvirt-backup-system verify` against the local repo. Cross-host
verify is opt-in via `libvirt-backup-system verify --include-hosts=...` and is
not scheduled by default.

## Preflight estimate

```
SPACE_MARGIN_PERCENT=20
BACKUP_ESTIMATE_GB_PER_VM=1
BACKUP_INCREMENTAL_MULTIPLIER=1.2
```

Free-space margin, per-VM fallback estimate (in GB) used when disk
introspection fails, and multiplier applied to the sum of VM disk virtual
sizes. The estimate is a worst-case bound on first-run space; later runs
generally need far less because Kopia dedup absorbs unchanged chunks.
