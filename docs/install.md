# Install and prerequisites

## Prerequisites

The CLI shells out to `virsh`, `qemu-nbd`, `nbdcopy`, `qemu-img`, `df`, and
`kopia`. On a KVM host the libvirt and qemu tooling is usually already
present; `kopia` and `nbdcopy` are the pieces you typically need to add.

### Debian 12 (bookworm) and Debian 13 (trixie)

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils libnbd-bin
```

`libnbd-bin` provides `nbdcopy`; `qemu-utils` provides `qemu-img` and
`qemu-nbd`.

### Ubuntu 22.04 (jammy), 24.04 (noble)

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils libnbd-bin
```

### Install kopia

Pin a known-good version and verify the SHA256 before installing. See
[kopia releases](https://github.com/kopia/kopia/releases) for the latest
pinned version.

Debian/Ubuntu via apt repo:

```sh
curl -fsSL https://kopia.io/signing-key | sudo gpg --dearmor -o /etc/apt/keyrings/kopia-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kopia-keyring.gpg] http://packages.kopia.io/apt/ stable main" \
  | sudo tee /etc/apt/sources.list.d/kopia.list
sudo apt update
sudo apt install -y kopia
```

Direct download (matches what production hosts run; pin both URL and SHA256
when bumping):

```sh
curl -fL -o /tmp/kopia.tar.gz https://github.com/kopia/kopia/releases/download/v0.21.1/kopia-0.21.1-linux-x64.tar.gz
echo '<sha256>  /tmp/kopia.tar.gz' | sha256sum -c -
sudo tar -xzf /tmp/kopia.tar.gz -C /usr/local/bin --strip-components=1 kopia-0.21.1-linux-x64/kopia
```

After installing this project, run `sudo libvirt-backup-system check` to
confirm every required binary resolves before relying on scheduled backups.

## Install

Pick a shared password once and use it on every host. The exact same install
command runs everywhere:

```sh
sudo env BACKUP_PATH=/mnt/qnap-backups python3 -m libvirt_backup_system install \
     --kopia-password "$(openssl rand -base64 32)"
```

For operators who do not want the password in `ps`/journald, pipe it in:

```sh
echo -n "$PW" | sudo python3 -m libvirt_backup_system install --kopia-password-file=-
```

Or use an env var:

```sh
sudo KOPIA_PW="$PW" python3 -m libvirt_backup_system install --kopia-password-env=KOPIA_PW
```

Behavior:

- First install: writes the password to
  `/etc/libvirt-backup-system/kopia.pw` mode 600 root-owned, creates the
  local repo at `BACKUP_PATH/<host-id>/kopia-repo/`, applies the global
  retention/compression policy, registers systemd units.
- Re-running with the same password: idempotent.
- Re-running with a different password: hard fail. Use
  `libvirt-backup-system change-password` to rotate.
- Re-running with no password flag and an existing password file: keep using
  the existing file (useful for refreshing systemd units without re-typing).

Or install first, edit the environment file, validate it, and then start the
timer:

```sh
sudo python3 -m libvirt_backup_system install --kopia-password=<value>
sudoedit /etc/libvirt-backup-system/libvirt-backup.env
sudo libvirt-backup-system check
sudo libvirt-backup-system start
sudo libvirt-backup-system doctor
```

The first install leaves `BACKUP_PATH` blank unless it is supplied in the
environment. When `BACKUP_PATH` is blank, systemd unit installation is
skipped. Run `start` after setting it so the service gets the matching
`RequiresMountsFor=` dependency and the timer is activated.

Only `BACKUP_PATH` is honored from the process environment during a first
install — other keys (for example `HOST_ID`, `BACKUP_REQUIRE_NFS_MOUNT`,
`KOPIA_COMPRESSION`) are written as commented defaults in
`libvirt-backup.env` and are silently ignored at install time, because the
systemd unit only reads the env file. Set those by editing
`libvirt-backup.env`; run `start` after changing values that affect unit
rendering, such as `BACKUP_PATH` or `SYSTEMD_ON_CALENDAR`.

The installer creates:

- `/opt/libvirt-backup-system`
- `/usr/local/bin/libvirt-backup-system`
- `/etc/libvirt-backup-system/libvirt-backup.env`
- `/etc/libvirt-backup-system/kopia.pw` (mode 600 root-owned)
- `/usr/share/fish/vendor_completions.d/libvirt-backup-system.fish`
- `/var/lib/libvirt-backup-system/kopia-configs/` (per-repo Kopia config
  files)
- `/var/cache/libvirt-backup-system/kopia/` (Kopia chunk cache)

When `BACKUP_PATH` is configured, it also creates:

- `/etc/systemd/system/libvirt-backup-system.service`
- `/etc/systemd/system/libvirt-backup-system-check.service`
- `/etc/systemd/system/libvirt-backup-system.timer`

### Shell completion

The fish completion file is auto-installed by `install` and removed by
`uninstall`; fish picks it up from `/usr/share/fish/vendor_completions.d/`
without any `source` line in `config.fish`. TAB at any subcommand position
offers the available subcommands and flags. The completion file ships as
package data inside `libvirt-backup-system` itself, so the install path needs
no PyPI access — it is a plain file copy.

If fish is not installed the file is still written; it just sits unused until
the operator installs fish.

#### Dynamic restore completion

`sudo libvirt-backup-system restore <TAB>` queries `list-restore-points` and
suggests the available VM UUIDs (with the VM name in the description). After
picking a UUID, a second `<TAB>` lists the timestamps recorded for that VM so
the operator can pick the right run from the menu.

The query runs `sudo -n libvirt-backup-system list-restore-points` so the
completion never prompts for a password mid-TAB: it relies on the sysadmin's
active sudo token. When the token has lapsed the completion silently falls
back to a non-sudo invocation; on a default install where the env file is
mode `0600 root:root` that fallback produces no rows, and the operator
either re-runs `sudo true` to refresh the token or copy-pastes from a
`sudo libvirt-backup-system list-restore-points` run instead.

The default timer is controlled by `SYSTEMD_ON_CALENDAR=*-*-* 02:30:00`.
`install` writes the units when `BACKUP_PATH` is already configured, but
does not enable the timer. `start` re-renders the units from the current
environment file, reloads systemd, and enables/starts the timer after
`check` has passed. Activating the timer does not run a backup immediately;
the next backup waits for the configured schedule unless you run
`sudo libvirt-backup-system run`.

When the systemd units are installed, `sudo libvirt-backup-system run` and
`sudo libvirt-backup-system check` dispatch through the corresponding
`.service` unit (via `systemctl start --wait`) so the ad-hoc invocation runs
in the exact environment the scheduled timer uses — same `EnvironmentFile=`,
`RequiresMountsFor=`, `StateDirectory=`, and hardening directives. The unit's
output is replayed to the operator's terminal by filtering the journal on the
run's `InvocationID`.

For `run`, the timer must already be loaded, enabled, and active. If a
systemd host has not successfully run `start`, manual `run` exits nonzero
with a "backup service is not running" error instead of starting an
unregistered ad-hoc backup.

Dispatch is automatically skipped for `check`/`preflight` (the subcommand
runs in-process instead) when any of these hold:

- `--prefix` is passed (install rooted elsewhere)
- `--config` is passed (the unit's `ExecStart` has the config path baked in)
- The unit file is not on disk yet (fresh checkout, package not deployed)
- `systemctl` is unavailable on the host
- `INVOCATION_ID` is set in the environment (systemd is already running the
  unit, so the orchestrator does not recurse)
- `LIBVIRT_BACKUP_NO_SYSTEMD_DISPATCH=1` is set in the environment

## Multi-host cutover

When deploying to a fleet from a previous (non-kopia) install, see the
[Kopia repo doc](kopia.md#multi-host-cutover) for the per-host checklist.

## Uninstall

```sh
sudo libvirt-backup-system uninstall
```

Uninstall removes installed program files and systemd units. Config, state,
logs, the Kopia password file, and the on-disk repo are preserved unless
`--purge-*` flags are passed. The repo directory under `BACKUP_PATH` is never
touched by uninstall — delete it by hand once the operator is sure the
backups are no longer needed.
