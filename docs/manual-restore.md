# Manual restore process

Most operators should use the built-in `list-restore-points` and `restore`
subcommands:

```sh
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
```

`list-restore-points` prints every recorded run across every per-host repo
under `BACKUP_PATH`. The first two columns are the VM UUID and the per-run
timestamp; copy that pair straight into `restore`. `restore` connects to the
matching host's kopia repo with the shared password, reads the meta
snapshot's `manifest.json`, materializes each disk snapshot through
`qemu-img convert -f raw -O qcow2 -S 4096`, and re-defines the domain.

The wrapper is quiet by default: it prints summary success/error events.
Use `sudo libvirt-backup-system restore --verbose ...` for per-disk
progress.

This page covers the manual procedure for situations where the operator
needs full control over each step (off-host recovery onto a freshly
provisioned KVM host, recovering selected files out of a snapshot without
defining a VM, debugging a corrupted snapshot, etc).

## Repo layout

Each host writes to its own repo:

```
BACKUP_PATH/<host-id>/kopia-repo/
  kopia.repository.f       # repo sentinel
  _log_*, _v*, indexes/    # kopia internals
  p<...>/                  # encrypted, deduplicated chunks
```

All repos share the same password. The password file lives at
`$KOPIA_PASSWORD_FILE` (default `/etc/libvirt-backup-system/kopia.pw`,
mode 600 root-owned). See [Kopia operations](kopia.md) for the details on
repo identity, peer discovery, and password recovery.

## Manual recovery outline

Pick the source host's repo on the NFS mount:

```sh
SRC=/mnt/qnap-backups/host-a/kopia-repo
PW=/etc/libvirt-backup-system/kopia.pw
```

Connect a local kopia config-file to the repo read-only:

```sh
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     repository connect filesystem --path "$SRC" --readonly
```

List the meta snapshots for the VM you want to restore:

```sh
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     snapshot list --all --json --tags=vm-uuid:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa --tags=kind:meta
```

Each entry's `id` is a kopia snapshot ID. Pick the one whose `startTime`
matches the run you want.

Pull the meta snapshot into a staging dir:

```sh
sudo mkdir -p /var/tmp/lbs-restore/meta
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     snapshot restore <meta-snap-id> /var/tmp/lbs-restore/meta
cat /var/tmp/lbs-restore/meta/manifest.json
```

The manifest names the run id, the disks, and embeds the domain XML.
For each disk in the manifest, find its disk snapshot via the shared
`run-id` tag:

```sh
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     snapshot list --all --json \
       --tags=run-id:<run-id-from-manifest> \
       --tags=kind:disk \
       --tags=disk:vda
```

Pipe the disk snapshot's `vda.raw` file through `qemu-img convert` to get a
sparse qcow2 on local storage:

```sh
sudo mkdir -p /var/tmp/lbs-restore/disks
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     snapshot restore <disk-snap-id>/vda.raw - \
   | sudo qemu-img convert -f raw -O qcow2 -S 4096 - /var/tmp/lbs-restore/disks/vda.qcow2
```

Repeat for each disk in the manifest.

To verify a single snapshot before relying on it:

```sh
sudo KOPIA_PASSWORD="$(cat "$PW")" kopia \
     --config-file=/tmp/lbs-manual.config \
     snapshot verify --max-failures=0 <snap-id>
```

After the disks are on local storage, move them to the intended libvirt
storage location, edit the manifest's domain XML to point at the new paths
(or to renumber NIC MACs / change the network if you are running this side-
by-side with the original), and `virsh define` the XML.

## When the wrapper is faster

The `restore` subcommand does exactly the steps above with safety rails:
exclusive run-lock, atomic staging-dir creation, automatic discovery of the
peer repo, manifest schema validation, sparse-qcow2 conversion, and
domain redefine. Use this manual procedure only when you genuinely need
something the wrapper does not do.
