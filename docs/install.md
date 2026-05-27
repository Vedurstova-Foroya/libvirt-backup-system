# Install and prerequisites

## Prerequisites

The CLI shells out to `virsh`, `qemu-nbd`, `nbdcopy`, `qemu-img`, `df`, and
`kopia`. The installer takes care of `kopia` and `nbdcopy`
automatically (see [Bundled binary install](#bundled-binary-install)
below); you only need the libvirt + qemu tooling on the host beforehand.

### Debian 12 (bookworm) and Debian 13 (trixie)

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils
```

`qemu-utils` provides `qemu-img` and `qemu-nbd`.

### Ubuntu 22.04 (jammy), 24.04 (noble)

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils
```

### Bundled binary install

On first install the orchestrator fetches a pinned `kopia` tarball from
the upstream GitHub release page and a pinned `libnbd-bin` (plus
`libnbd0`) `.deb` from the Debian archive, sha256-verifies each artifact
against a constant baked into `libvirt_backup_system/installer_binaries.py`,
and installs them via an atomic move (`kopia` to `/usr/local/bin/kopia`)
and `dpkg -i` (with `apt-get install -f` fallback for `libnbd-bin`). The
step is idempotent: if `kopia --version` already reports the pinned
version and `nbdcopy --version` runs successfully, the install skips the
network round-trip entirely. Bumping the pinned versions is a deliberate
operator action — the matching sha256 must be refreshed in the same
commit.

After installing this project, run `sudo libvirt-backup-system check` to
confirm every required binary resolves before relying on scheduled backups.

### Offline / air-gapped install

When outbound HTTPS to `github.com` / `deb.debian.org` is not available,
pre-place the binaries by hand and the installer will detect them and
skip the bundled-install step:

```sh
# kopia: download elsewhere, verify the upstream sha256, then copy to
# /usr/local/bin/kopia (mode 0755 root-owned) at the version the
# installer pins.
sudo install -m 0755 -o root -g root /path/to/kopia /usr/local/bin/kopia
kopia --version

# nbdcopy: install via the host's package manager once a mirror is
# available, or copy + dpkg -i the pinned .debs you mirrored ahead of time.
sudo apt install -y libnbd-bin
nbdcopy --version
```

With both binaries present and runnable, `sudo libvirt-backup-system install`
proceeds straight to the password + repo + systemd-unit setup.

## Install

Pick a shared password once, store it in your secrets vault, and use it on
every host. The exact same install command runs everywhere:

```sh
export KOPIA_PW='<shared-password-from-vault>'
sudo env BACKUP_PATH=/mnt/qnap-backups KOPIA_PW="$KOPIA_PW" \
     python3 -m libvirt_backup_system install \
     --kopia-password-env KOPIA_PW \
     --acknowledge-password-loss
```

For operators who do not want the password in `ps`/journald, pipe it in:

```sh
echo -n "$PW" | sudo python3 -m libvirt_backup_system install --kopia-password-file=- \
  --acknowledge-password-loss
```

Or use an env var:

```sh
sudo env KOPIA_PW="$PW" python3 -m libvirt_backup_system install --kopia-password-env=KOPIA_PW \
  --acknowledge-password-loss
```

Behavior:

- First install: writes the password to
  `/etc/libvirt-backup-system/kopia.pw` mode 600 root-owned, creates the
  local repo at `BACKUP_PATH/<host-id>/kopia-repo/`, applies the global
  retention/compression policy, registers systemd units. It refuses to write
  a new password unless `--acknowledge-password-loss` is present; refusal
  messages do not print the password, so store the exact value in your
  secrets vault before running install.
- Re-running with the same password: idempotent.
- Re-running with a different password: hard fail. Use
  `libvirt-backup-system change-password` to rotate.
- Re-running with no password flag and an existing password file: keep using
  the existing file (useful for refreshing systemd units without re-typing).

Or install first, edit the environment file, validate it, and then start the
timer:

```sh
sudo python3 -m libvirt_backup_system install --kopia-password=<value> \
  --acknowledge-password-loss
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
- `/etc/systemd/system/libvirt-backup-system-maintenance.service`
- `/etc/systemd/system/libvirt-backup-system-maintenance.timer`
- `/etc/systemd/system/libvirt-backup-system-maintenance-full.service`
- `/etc/systemd/system/libvirt-backup-system-maintenance-full.timer`
- `/etc/systemd/system/libvirt-backup-system-verify.service`
- `/etc/systemd/system/libvirt-backup-system-verify.timer`

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
suggests the available VM UUIDs with the source host and restore-point count
in the description. After picking a UUID, a second `<TAB>` lists the
timestamps recorded for that VM so the operator can pick the right run from
the menu.

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
environment file, reloads systemd, and enables/starts
`libvirt-backup-system.timer`, `libvirt-backup-system-maintenance.timer`,
`libvirt-backup-system-maintenance-full.timer`, and
`libvirt-backup-system-verify.timer` after `check` has passed. Activating
the backup timer does not run a backup immediately;
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
