# libvirt-backup-system

Python CLI for backing up libvirt VMs with `virtnbdbackup` into a configured monthly backup tree such as a mounted QNAP NFS export.

## Prerequisites

The CLI shells out to `virsh`, `virtnbdbackup`, `virtnbdrestore`, `qemu-img`, and `df`. On a KVM host the libvirt and qemu tooling is usually already present; `virtnbdbackup` is the piece you typically need to add.

### Debian 12 (bookworm) and Debian 13 (trixie)

```sh
sudo apt update
sudo apt install -y libvirt-clients virtnbdbackup
```

The `virtnbdbackup` package pulls in `qemu-utils` (for `qemu-img`) automatically.

### Ubuntu 22.04 (jammy)

`virtnbdbackup` is not packaged for 22.04. Install runtime deps from apt, then the upstream `.deb` from the [virtnbdbackup releases page](https://github.com/abbbi/virtnbdbackup/releases). Verify the SHA256 before installing so a tampered release artifact cannot be installed as root:

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

After installing, run `sudo libvirt-backup-system check` to confirm every required binary resolves before continuing with `install`.

## Install

From a checkout on a Debian/Ubuntu-style KVM host:

```sh
sudo python3 -m libvirt_backup_system install
```

Edit `/etc/libvirt-backup-system/libvirt-backup.env` and set `BACKUP_PATH`.
It is intentionally blank after the first install. The installer skips systemd
unit installation until this path is set; re-run `install` after editing so the
service gets the matching `RequiresMountsFor=` dependency.

```sh
sudo python3 -m libvirt_backup_system install
sudo libvirt-backup-system check
sudo libvirt-backup-system run
```

The installer creates:

- `/opt/libvirt-backup-system`
- `/usr/local/bin/libvirt-backup-system`
- `/etc/libvirt-backup-system/libvirt-backup.env`
- `/etc/systemd/system/libvirt-backup-system.service`
- `/etc/systemd/system/libvirt-backup-system.timer`

The default timer is controlled by `SYSTEMD_ON_CALENDAR=*-*-* 02:30:00`.

## Uninstall

```sh
sudo libvirt-backup-system uninstall
```

Uninstall removes installed program files and systemd units. Config, state, logs, and backups are preserved unless purge flags are passed.

## Commands

- `install`
- `uninstall`
- `check`
- `preflight`
- `run`
- `list-vms`
- `verify`
- `cleanup`
- `restore-to-dir`

## Tests

Adaptive end-to-end runner:

```sh
python3 -m tests.e2e
```

The runner always attempts the Docker Compose orchestration test. A real KVM test is auto-skipped unless the host is Linux, Docker is available, `/dev/kvm` exists, and a privileged probe container can access `/dev/kvm`.
