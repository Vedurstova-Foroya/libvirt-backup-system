# Install and prerequisites

## Prerequisites

The CLI shells out to `virsh`, `virtnbdbackup`, `virtnbdrestore`, `qemu-img`, and `df`. On a KVM host the libvirt and qemu tooling is usually already present; `virtnbdbackup` is the piece you typically need to add.

### Debian 12 (bookworm) and Debian 13 (trixie)

```sh
sudo apt update
sudo apt install -y libvirt-clients virtnbdbackup
```

The `virtnbdbackup` package pulls in `qemu-utils` for `qemu-img` automatically.

### Ubuntu 22.04 (jammy)

`virtnbdbackup` is not packaged for 22.04. Install runtime dependencies from apt, then the upstream `.deb` from the [virtnbdbackup releases page](https://github.com/abbbi/virtnbdbackup/releases). Verify the SHA256 before installing so a tampered release artifact cannot be installed as root:

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils python3-libvirt python3-libnbd libnbd-bin nbdkit nbdkit-plugin-python
curl -fL -o /tmp/virtnbdbackup.deb https://github.com/abbbi/virtnbdbackup/releases/download/v2.46/virtnbdbackup_2.46-1_all.deb
echo 'b839ed328f49cb3f44d5bb78124cec7eac596d9812400935758483acd3be38ea  /tmp/virtnbdbackup.deb' | sha256sum -c -
sudo apt install -y /tmp/virtnbdbackup.deb
```

When bumping to a newer release, refresh the URL and the pinned SHA256 together.

### Ubuntu 24.04 (noble)

Noble ships an older `virtnbdbackup` (2.0). Prefer the upstream `.deb`, verifying the SHA256 before install:

```sh
sudo apt update
sudo apt install -y libvirt-clients qemu-utils python3-libvirt python3-libnbd libnbd-bin nbdkit nbdkit-plugin-python
curl -fL -o /tmp/virtnbdbackup.deb https://github.com/abbbi/virtnbdbackup/releases/download/v2.46/virtnbdbackup_2.46-1_all.deb
echo 'b839ed328f49cb3f44d5bb78124cec7eac596d9812400935758483acd3be38ea  /tmp/virtnbdbackup.deb' | sha256sum -c -
sudo apt install -y /tmp/virtnbdbackup.deb
```

After installing this project, run `sudo libvirt-backup-system check` to confirm every required binary resolves before relying on scheduled backups.

## Install

For a one-command install from a checkout:

```sh
sudo env BACKUP_PATH=/mnt/qnap-backups python3 -m libvirt_backup_system install
```

Or install first, edit the environment file, and re-run install:

```sh
sudo python3 -m libvirt_backup_system install
sudoedit /etc/libvirt-backup-system/libvirt-backup.env
sudo python3 -m libvirt_backup_system install
sudo libvirt-backup-system check
```

The first install leaves `BACKUP_PATH` blank unless it is supplied in the environment. When `BACKUP_PATH` is blank, systemd unit installation is skipped. Re-run `install` after setting it so the service gets the matching `RequiresMountsFor=` dependency.

Only `BACKUP_PATH` is honored from the process environment during a first install — other keys (for example `HOST_ID`, `BACKUP_REQUIRE_NFS_MOUNT`, `BACKUP_COMPRESS`) are written as commented defaults in `libvirt-backup.env` and are silently ignored at install time, because the systemd unit only reads the env file. Set those by editing `libvirt-backup.env` and re-running `install`.

The installer creates:

- `/opt/libvirt-backup-system`
- `/usr/local/bin/libvirt-backup-system`
- `/etc/libvirt-backup-system/libvirt-backup.env`

When `BACKUP_PATH` is configured, it also creates:

- `/etc/systemd/system/libvirt-backup-system.service`
- `/etc/systemd/system/libvirt-backup-system-check.service`
- `/etc/systemd/system/libvirt-backup-system.timer`

The default timer is controlled by `SYSTEMD_ON_CALENDAR=*-*-* 02:30:00`.

When the systemd units are installed, `sudo libvirt-backup-system run` and `sudo libvirt-backup-system check` dispatch through the corresponding `.service` unit (via `systemctl start --wait`) so the ad-hoc invocation runs in the exact environment the scheduled timer uses — same `EnvironmentFile=`, `RequiresMountsFor=`, `StateDirectory=`, and hardening directives. The unit's output is replayed to the operator's terminal by filtering the journal on the run's `InvocationID`.

Dispatch is automatically skipped (the subcommand runs in-process instead) when any of these hold:

- `--prefix` is passed (install rooted elsewhere)
- `--config` is passed (the unit's `ExecStart` has the config path baked in)
- The unit file is not on disk yet (fresh checkout, package not deployed)
- `systemctl` is unavailable on the host
- `INVOCATION_ID` is set in the environment — this is what systemd sets when the unit itself is running, so the orchestrator does not recurse into a second dispatch
- `LIBVIRT_BACKUP_NO_SYSTEMD_DISPATCH=1` is set in the environment (explicit operator opt-out for development or recovery)

## Uninstall

```sh
sudo libvirt-backup-system uninstall
```

Uninstall removes installed program files and systemd units. Config, state, logs, and backups are preserved unless purge flags are passed.
