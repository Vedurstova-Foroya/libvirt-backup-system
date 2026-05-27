# Command reference

## `install`

Installs the package copy, wrapper script, config file, fish completion, and
systemd units when `BACKUP_PATH` is configured. Writes the shared kopia
password to `/etc/libvirt-backup-system/kopia.pw` (mode 600 root-owned) and
creates the local kopia repo at `BACKUP_PATH/<host-id>/kopia-repo/` with the
global retention/compression policy applied.

```sh
sudo libvirt-backup-system install --kopia-password=<value> --acknowledge-password-loss
sudo libvirt-backup-system install --kopia-password-file=/path/to/file --acknowledge-password-loss
echo -n "$PW" | sudo libvirt-backup-system install --kopia-password-file=- --acknowledge-password-loss
sudo env KOPIA_PW=... libvirt-backup-system install --kopia-password-env=KOPIA_PW --acknowledge-password-loss
```

On first install, `--acknowledge-password-loss` is required before a newly
supplied password is written. Without it, the command exits nonzero with a
secret-free error; store the exact value in a secrets vault before running
install.

The same install command runs on every host. There is no bootstrap host.
Idempotent if the supplied password matches the existing file; hard-fails if
it does not (use `change-password` to rotate). The timer is not enabled
automatically — run `check` and then `start` after editing the env file.

When run via the installed wrapper the package files at
`/opt/libvirt-backup-system` are kept as-is (refreshing them mid-execute
would delete the source being copied). The wrapper, env file, and systemd
units are still refreshed. To pick up new package code, run `install` from a
source checkout instead:

```sh
sudo python3 -m libvirt_backup_system install --kopia-password-file=/etc/libvirt-backup-system/kopia.pw
```

## `change-password`

Rotates the kopia repo password on this host. Read the current password,
verify it decrypts the local repo, run `kopia repository change-password` to
rewrap the master key, atomically replace the password file.

```sh
sudo libvirt-backup-system change-password --new-kopia-password=<value>
sudo libvirt-backup-system change-password --new-kopia-password-file=/path
echo -n "$PW" | sudo libvirt-backup-system change-password --new-kopia-password-file=-
sudo env NEW_KOPIA_PW=... libvirt-backup-system change-password --new-kopia-password-env=NEW_KOPIA_PW
```

Run the same command on every host. Order does not matter; each host rotates
its own local repo independently. `doctor` flags any host still holding the
old value. See [Kopia operations](kopia.md#password-rotation) for the full
recovery procedure.

## `uninstall`

Removes installed program files and systemd units. Config, state, logs, the
kopia password file, and the on-disk repo are preserved unless purge flags
are passed.

```sh
sudo libvirt-backup-system uninstall
sudo libvirt-backup-system uninstall --purge-config --purge-state --purge-logs
```

## `check` / `preflight`

Validates config, required binaries (`virsh`, `qemu-nbd`, `nbdcopy`,
`qemu-img`, `df`, `kopia`), root execution policy, VM discovery, backup path
writability, the kopia password file mode/owner, and estimated free space.

```sh
sudo libvirt-backup-system check
```

## `doctor`

Diagnoses install registration, runtime state, and the same preflight
surface that `check` covers. Specifically, `doctor` is a superset of
`check` — it runs the full preflight layer and then appends:

- Wrapper script, package directory, and config file are in place.
- All systemd unit and timer files exist on disk with content matching what a
  fresh `install` would render (catches drift after editing the env file
  without re-running install).
- Backup, maintenance, full-maintenance, and verify timers are enabled and
  active.
- Last `libvirt-backup-system.service` run completed cleanly.
- Local kopia repo connects with the shared password and
  `kopia repository status` is clean.
- Local `kopia maintenance run --dry-run` and `kopia snapshot verify
  --dry-run` complete cleanly.
- Every peer repo under `BACKUP_PATH/*/kopia-repo/` is reachable read-only
  with the shared password (cross-host-restore smoke test).

```sh
sudo libvirt-backup-system doctor
```

## `start`

Installs or refreshes the systemd unit files from the current environment
file, reloads systemd, refreshes the kopia global retention/compression
policy, and enables/starts `libvirt-backup-system.timer`,
`libvirt-backup-system-maintenance.timer`,
`libvirt-backup-system-maintenance-full.timer`, and
`libvirt-backup-system-verify.timer`. Activates the schedules only; does
not run a backup immediately. Use after `install`, after editing
`/etc/libvirt-backup-system/libvirt-backup.env`, and after `check` has
passed.

```sh
sudo libvirt-backup-system start
```

## `run`

Runs preflight, acquires the run lock, and backs up every running VM.
Offline VMs are logged as `skipping vm because it is offline` and skipped.
Each VM produces one kopia disk snapshot per disk plus one meta snapshot
carrying the run manifest (domain XML, disk table, run id). Snapshots are
tagged with `vm-uuid`, `run-id`, `disk`, `host`, and `kind`; meta snapshots
also carry `vm-name` and `timestamp` for restore-point listings.

Manual backups require the systemd schedule to have been activated first
with a successful `start`. On a systemd host, `run` exits nonzero with a
"backup service is not running" error instead of starting an ad-hoc backup
when the service/timer has not been installed and activated.

Pruning is handled by the kopia maintenance timer, not by the backup loop —
a slow GC pass cannot delay backups.

```sh
sudo libvirt-backup-system run
```

## `status`

Prints `systemctl status` for the backup timer/service, check service,
maintenance timer/service, and verify timer/service units. Output is the raw
human-readable systemctl output (not JSON), so the next-fire time, last-run
result, and any recent journal lines are visible at a glance. Exit code is the
worst (highest) systemctl return code across those units, so unloaded units
propagate as failure.

```sh
sudo libvirt-backup-system status
```

## `list-vms`

Lists selected VMs after applying `VM_BLACKLIST` (UUID-based).

```sh
sudo libvirt-backup-system list-vms
sudo libvirt-backup-system list-vms --json
sudo libvirt-backup-system list-vms --include-blacklisted
```

## `verify`

Runs `kopia snapshot verify` against the local repo by default. Cross-host
verification is opt-in via `--include-hosts`.

```sh
sudo libvirt-backup-system verify
sudo libvirt-backup-system verify --include-hosts=host-a,host-b
```

`VM_BLACKLIST` is intentionally ignored: verifying blacklisted-VM backups is
still useful.

## `list-restore-points`

Walks every per-host repo under `BACKUP_PATH/*/kopia-repo/`, connects
read-only with the shared password, lists `kind:meta` snapshots, and prints
one row per (host, VM UUID, timestamp). Copy the `vm-uuid` and `timestamp`
columns straight into `restore`.

```sh
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system list-restore-points | grep my-vm
sudo libvirt-backup-system list-restore-points | less -S
```

Output columns:

```
source-host-id  vm-uuid  vm-name  timestamp  run-id
```

`source-host-id` is where the backup was taken. `run-id` joins the meta
snapshot to its disk snapshots for diagnostics and manual operations (see
[Kopia operations](kopia.md)).

## `restore`

Restores a single backup run identified by the `(vm_uuid, timestamp)` pair
from `list-restore-points`. The action is automatic:

- If the backup was taken on this host **and** a libvirt domain with that
  UUID exists locally, the VM is shut down, undefined, and redefined from
  the backup (in-place overwrite).
- Otherwise the VM is staged and redefined from the backup XML on this host
  (turnkey one-click recovery on a different host or after the local VM has
  been removed).

```sh
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
sudo libvirt-backup-system restore aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 20260507T101112
```

The only restore flag is `-v`/`--verbose`; there is still no
`--source-host`. The timestamp is the exact per-run target (no rounding, no
closest-match). For same-host restores where a local domain with the same
UUID exists, disks are restored to temporary sibling files first, then the
VM is shut down, undefined, and the temporary files replace the original
per-disk source paths. Cross-host or fresh restores write qcow2s under
`/var/lib/libvirt-backup-system/restore/<uuid>-<timestamp>/` and rewrite the
restored domain XML to those staged paths.

Internally each disk snapshot is piped through `qemu-img convert -f raw -O
qcow2 -S 4096` to produce a sparse qcow2 on the destination. The meta
snapshot is materialized to a tmp dir so the manifest's domain XML can be
read into `virsh define`. By default, restore prints only summary
success/error events; pass `-v`/`--verbose` to stream per-disk progress.

### Cross-host recovery

`list-restore-points` walks every host directory under `BACKUP_PATH`, not
just the current `HOST_ID`, so a recovery host that mounted the backup tree
sees every host's snapshots. `restore` follows the same path: it picks up
the snapshot from whichever host's repo contains a matching `(uuid,
timestamp)`. When that host does not match the local one (or no local VM
with that UUID exists), the turnkey define path runs.

There is no `--source-host` flag. The shared password decrypts every host's
repo, so cross-host restore is the same command as same-host restore.

### How snapshots are tagged

Each backup run produces:

- One **disk snapshot** per disk, tagged
  `kind=disk vm-uuid=<uuid> run-id=<uuid> disk=<target> host=<host-id>`,
  containing one logical file `<target>.raw`.
- One **meta snapshot** tagged `kind=meta vm-uuid=<uuid> vm-name=<name>
  timestamp=<YYYYMMDDTHHMMSS> run-id=<uuid> host=<host-id>`, containing
  `manifest.json` (VM name, UUID, run id, timestamp, libvirt URI, domain XML,
  disk table).

`restore` resolves a meta snapshot by `(vm-uuid, timestamp)`, reads the
manifest, then looks up each disk snapshot by `run-id + disk=<target>`.
See [Kopia operations](kopia.md#tag-schema) for the full tag schema.

## Retention

Retention is enforced by the kopia global policy
(`KEEP_LATEST/HOURLY/DAILY/WEEKLY/MONTHLY/ANNUAL`), refreshed from the env
file on every `start`. The maintenance timer
(`KOPIA_MAINTENANCE_INTERVAL`, default `24h`) prunes expired snapshots and
compacts the repo. See [Kopia operations](kopia.md#maintenance) for manual
maintenance commands.
