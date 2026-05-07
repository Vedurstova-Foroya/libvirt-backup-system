# libvirt-backup-system

Python CLI for backing up libvirt VMs with `virtnbdbackup`, staging local monthly backup trees, and syncing them to an SSH/rsync target such as a QNAP.

## Install

From a checkout on a Debian/Ubuntu-style KVM host:

```sh
sudo python3 -m libvirt_backup_system install
```

Edit `/etc/libvirt-backup-system/libvirt-backup.env`, then run:

```sh
sudo libvirt-backup-system preflight
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
