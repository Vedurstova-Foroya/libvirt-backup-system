# Command reference

## `install`

Installs the package copy, wrapper script, config file, fish completion, and
systemd units when `BACKUP_PATH` is configured. Writes the shared kopia
token to `/etc/libvirt-backup-system/kopia.pw` (mode 600 root-owned) and
creates the local kopia repo at `BACKUP_PATH/<host-id>/kopia-repo/` with the
global retention/compression policy applied.

```sh
sudo env BACKUP_PATH=/home/admin/pro/vms/backups libvirt-backup-system install
sudo libvirt-backup-system install --kopia-password=<value> --acknowledge-password-loss
sudo libvirt-backup-system install --kopia-password-file=/path/to/file --acknowledge-password-loss
echo -n "$PW" | sudo libvirt-backup-system install --kopia-password-file=- --acknowledge-password-loss
sudo env KOPIA_PW=... libvirt-backup-system install --kopia-password-env=KOPIA_PW --acknowledge-password-loss
```

With no `--kopia-password*` flag and no password file, `install` generates the
shared token automatically. Explicit first writes still require
`--acknowledge-password-loss`. Re-running install is idempotent with the same
token and fails on mismatch; joining with the wrong token also fails when peer
repos already exist.

When `BACKUP_PATH` is set, a fresh install also syncs the shared config seed at
`BACKUP_PATH/libvirt-backup.env`: it publishes this host's config if no seed
exists yet (first node), or pulls the existing seed as the initial local config
(joining node). See [`update-config`](#update-config) and
[Joining additional hosts](joining-hosts.md#shared-configuration).

## `add-node`

Prints a pasteable command for joining another host to the same `BACKUP_PATH`
and shared token:

```sh
sudo libvirt-backup-system add-node
```

```sh
sudo env BACKUP_PATH=... KOPIA_PW=... python3 -m libvirt_backup_system install --kopia-password-env KOPIA_PW --acknowledge-password-loss
```

Run it on the new host from a checkout; see [Joining additional hosts](joining-hosts.md).

## `show-token`

Prints the raw shared token from the secure password file:

```sh
sudo libvirt-backup-system show-token
```

## `update-config`

Publishes this host's env file to the backup path as the shared config seed
(`BACKUP_PATH/libvirt-backup.env`), overwriting any previous seed:

```sh
sudoedit /etc/libvirt-backup-system/libvirt-backup.env
sudo libvirt-backup-system start          # apply locally
sudo libvirt-backup-system update-config  # publish for future joins
```

The shared config is a *seed*, not a live-synced file. The first node publishes
it automatically (during `install` when `BACKUP_PATH` is set, and on `start`);
a node joining the same `BACKUP_PATH` pulls it as its initial local config so it
inherits retention, splitter, compression, NFS policy, and the backup schedule
without re-typing them. After joining, the local config is independent — edit it
and run `start` to change only that host (its own timer, mount path, etc.)
without touching the seed.

Run `update-config` whenever you want this host's current config to become the
template that future joins inherit; the most recent `update-config` from any
host wins. `HOST_ID` is never shared (it scopes the per-host repo, so each node
keeps its own). See [Joining additional hosts](joining-hosts.md#shared-configuration).

## `change-password`

Rotates the kopia repo token on this host. Read the current token, verify it
decrypts the local repo, run `kopia repository change-password` to rewrap the
master key, atomically replace the password file.

```sh
sudo libvirt-backup-system change-password --new-kopia-password=<value>
sudo libvirt-backup-system change-password --new-kopia-password-file=/path
echo -n "$PW" | sudo libvirt-backup-system change-password --new-kopia-password-file=-
sudo env NEW_KOPIA_PW=... libvirt-backup-system change-password --new-kopia-password-env=NEW_KOPIA_PW
```

Run the same command on every host. Order does not matter; each host rotates
its own local repo independently. `doctor` flags any host still holding the
old value. See [Kopia password handling](kopia-password.md#password-rotation)
for the full recovery procedure.

Kopia rotation receives the resolved new value in Kopia's argv; avoid
running it where untrusted users can inspect process arguments.

## `uninstall`

Removes installed program files and systemd units. Config, state, logs, the
kopia password file, and the on-disk repo are preserved by default. The
purge flags only remove config, state, and logs; uninstall never removes the
Kopia password file or repo. If `KOPIA_PASSWORD_FILE` is configured inside
any purged config, state, or log path, uninstall preserves that file and the
parent directories needed to keep it in place.

```sh
sudo libvirt-backup-system uninstall
sudo libvirt-backup-system uninstall --purge-config --purge-state --purge-logs
```

## `check` / `preflight`

Validates config, binaries, root policy, VM discovery, backup path writability,
the password file, local Kopia repo connectivity, and free space. Run `start`
once after setting a new `BACKUP_PATH`; `check` expects the repo.

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
- Local kopia repo connects with the shared token and
  `kopia repository status` is clean.
- Local `kopia maintenance info` and a lightweight
  `kopia snapshot verify --verify-files-percent=0` complete cleanly.
  `doctor` uses `maintenance info` because Kopia does not expose a
  non-mutating `maintenance run --dry-run`; the scheduled timers run the
  actual maintenance commands.
- Every peer repo under `BACKUP_PATH/*/kopia-repo/` is reachable read-only
  with the shared token (cross-host-restore smoke test).

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
not run a backup immediately. Kopia maintenance and verify timers have
staggered activation-relative initial delays to avoid concurrent first-run
repo operations.
Use after `install`, after editing `/etc/libvirt-backup-system/libvirt-backup.env`,
and to initialize an empty `BACKUP_PATH`; then run `check`.

On the first node, `start` also publishes the shared config seed at
`BACKUP_PATH/libvirt-backup.env` if none exists yet (it never overwrites an
existing seed). Use [`update-config`](#update-config) to push later edits to
the seed for future joins.

```sh
sudo libvirt-backup-system start
```

## `run` / `backup`

Runs preflight, acquires the run lock, and backs up every running VM.
Offline VMs are logged as `skipping vm because it is offline` and skipped.
Each VM produces one kopia disk snapshot per disk plus one meta snapshot with
the run manifest and restore-point tags (`vm-name`, `timestamp`,
`consistency`). QEMU guest agent quiesce is attempted per VM and falls back to
a crash-consistent snapshot if quiesce is unavailable; see
[Backup consistency](backup-consistency.md).

Manual backups require the systemd schedule to have been activated first
with a successful `start`. On a systemd host, `run`/`backup` exits nonzero with a
"backup service is not running" error instead of starting an ad-hoc backup
when the service/timer has not been installed and activated.

On a systemd host the backup runs **in the background**: `run`/`backup`
dispatches the work to the `libvirt-backup-system.service` unit with
`systemctl start --no-block` and returns as soon as systemd accepts the job.
The backup then runs under systemd (PID 1), so it keeps running to completion
even if you log out, close the terminal, or drop the SSH session. Follow it
with `libvirt-backup-system log -f` and review past runs with
`libvirt-backup-system log`. When systemd is unavailable (or `--config` /
`--prefix` is set, or you are already inside the unit), the backup instead
runs in-process in the foreground.

Pruning is handled by the kopia maintenance timer, not by the backup loop —
a slow GC pass cannot delay backups.

```sh
sudo libvirt-backup-system run            # starts in the background
sudo libvirt-backup-system backup
sudo libvirt-backup-system log -f         # follow the running backup
```

## `log` / `logs`

Shows the systemd journal for the backup units, modeled on `docker logs`. By
default it prints the most recent 50 lines from `libvirt-backup-system.service`
(the backup orchestrator) and exits. Pass `-f`/`--follow` to keep the stream
open and print new lines as the background backup writes them — the same live
output a foreground run would show. Following is read-only: Ctrl-C stops the
journal tail, not the backup.

```sh
sudo libvirt-backup-system log            # last 50 lines of the backup run
sudo libvirt-backup-system log -f         # stream live, like docker logs -f
sudo libvirt-backup-system log -n 200     # last 200 lines
sudo libvirt-backup-system log -n all     # entire run history
sudo libvirt-backup-system log -f all     # follow backup + maintenance + verify
sudo libvirt-backup-system log verify     # the kopia verify unit's journal
```

- `-f`, `--follow` — stream new lines instead of exiting.
- `-n N`, `--lines N` — recent lines to show before following; a non-negative
  integer or `all`. Default: `50`.
- Trailing component (default `run`) picks which unit's journal to read:
  `run`, `check`, `maintenance`, `maintenance-full`, `verify`, or `all` (the
  backup, maintenance, full-maintenance, and verify units interleaved).

`log` reads the system journal, so it needs the same privileges as
`journalctl` (run under `sudo`, or as a member of `systemd-journal`/`adm`).

## `status`

Prints `systemctl status` for the backup timer/service, check service,
maintenance timer/service, full-maintenance timer/service, and verify
timer/service units. Output is the raw human-readable systemctl output (not
JSON), so the next-fire time, last-run result, and any recent journal lines
are visible at a glance. Exit code is the
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
read-only with the shared token, lists `kind:meta` snapshots, and prints
one row per (host, VM UUID, timestamp). Copy the `vm-uuid` and `timestamp`
columns straight into `restore`.

```sh
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system list-restore-points | grep my-vm
sudo libvirt-backup-system list-restore-points | less -S
```

Output columns:

```
source-host-id  vm-uuid  timestamp  run-id  consistency  vm-name
```

`source-host-id` is where the backup was taken. `run-id` joins the meta
snapshot to its disk snapshots for diagnostics and manual operations (see
[Kopia operations](kopia.md)). `consistency` is `filesystem`, `crash`, or
`unknown`; see [Backup consistency](backup-consistency.md).

## `du`

Shows backup usage. With no filters it reports actual filesystem usage for
each `BACKUP_PATH/<host>/kopia-repo/` and a total.

```sh
sudo libvirt-backup-system du
sudo libvirt-backup-system du host-a
sudo libvirt-backup-system du aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
sudo libvirt-backup-system du host-a aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
```

One drilldown argument can be either a host id or VM UUID; two arguments are
host id then VM UUID. Drilldowns report restore-point count, latest logical
VM size, latest consistency, and Kopia packed size; top-level remains physical
repo usage.

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

Pass `--host-id <source-host-id>` or `--run-id <run-id>` only when duplicate
rows share the same `(vm-uuid, timestamp)`. The timestamp is the exact
per-run target (no rounding, no closest-match). For same-host restores where
a local domain with the same UUID exists, disks are restored to temporary
sibling files first, then the VM is shut down, undefined, and the temporary
files replace the original per-disk source paths. Cross-host or fresh
restores write qcow2s under
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

The shared token decrypts every host's repo, so cross-host restore is the
same command as same-host restore unless duplicate restore points require a
`--host-id` or `--run-id` disambiguator.

### How snapshots are tagged

`restore` resolves a meta snapshot by `(vm-uuid, timestamp)`, reads the
manifest, then looks up each disk snapshot by `run-id + disk=<target>`.
See [Kopia operations](kopia.md#tag-schema) for the full tag schema,
including consistency metadata.

## Retention

Retention is enforced by the kopia global policy
(`KEEP_LATEST/HOURLY/DAILY/WEEKLY/MONTHLY/ANNUAL`), refreshed from the env
file on every `start`. Defaults keep the latest 8 snapshots plus hourly points
for 24h and daily points for one year. The maintenance timer
(`KOPIA_MAINTENANCE_INTERVAL`, default `24h`) prunes expired snapshots and
compacts the repo. See [Kopia operations](kopia.md#maintenance) for manual
maintenance commands.
