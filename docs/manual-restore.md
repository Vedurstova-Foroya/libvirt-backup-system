# Manual restore process

Most operators should use the built-in `list-restore-points` and `restore` subcommands:

```sh
sudo libvirt-backup-system list-restore-points
sudo libvirt-backup-system restore <vm-uuid> <timestamp>
```

`list-restore-points` prints every recorded run across all hosts and VMs; the first two columns are the UUID and the per-run timestamp. `restore` looks the pair up, holds the same run-lock as `run`, and invokes `virtnbdrestore -a restore -i <chain-dir> -o <output-dir> --until <checkpoint> --name <vm-name> -D` against the matching chain. The output directory is the original disk directory when the backup XML has one safe file-backed disk directory, otherwise a private staging directory is used. The `-D` flag asks `virtnbdrestore` to redefine the domain in libvirt; same-host restores destroy and undefine the existing VM first.

The wrapper is quiet by default: it captures virtnbdrestore's detailed output and prints only summary success/error events. Use `sudo libvirt-backup-system restore --verbose ...` when you need the full virtnbdrestore stream.

This page covers the manual procedure for situations where the source backup must first be staged onto local storage (e.g. NFS read-only, off-host recovery), or where the operator needs full control over each step.

Backups are stored as:

```text
BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/
```

The `<chain-id>` is the timestamp of the first backup in the monthly incremental chain. Running VMs accumulate per-run incrementals into the same chain directory; each run also appends its `{ts, checkpoint}` entry to `runs.jsonl` inside the chain dir (see [Command reference — How chains map to snapshots](commands.md#how-chains-map-to-snapshots) for the exact JSON-line schema). To restore a specific intermediate run by hand, read any line's `checkpoint` field from `runs.jsonl` and pass it to `virtnbdrestore --until <checkpoint>`; without `--until` the whole chain replays.

## Recovery outline

Pick the exact backup snapshot to recover — the chain directory under the desired calendar month:

```sh
SOURCE=/mnt/qnap-backups/myhost/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/2026-05/20260507T023000
```

Copy it to local storage on the recovery host:

```sh
sudo mkdir -p /var/tmp/libvirt-restore/my-vm-backup
sudo rsync -aH --numeric-ids "$SOURCE"/ /var/tmp/libvirt-restore/my-vm-backup/
```

Verify the copied backup before restoring from it (`-a` selects the action; `-o` is required even for verify but should point at a separate staging directory so a future upstream change cannot mutate the source backup):

```sh
sudo mkdir -p /var/tmp/libvirt-restore/verify-staging
sudo virtnbdrestore -a verify -i /var/tmp/libvirt-restore/my-vm-backup -o /var/tmp/libvirt-restore/verify-staging
```

Restore into an empty staging directory. `-o` is the restore *target directory* (not an action keyword), and `-D` is an optional boolean flag for registering the VM after restore — omit it unless you want `virtnbdrestore` to redefine the VM:

```sh
sudo mkdir -p /var/tmp/libvirt-restore/my-vm-restored
sudo virtnbdrestore -a restore -i /var/tmp/libvirt-restore/my-vm-backup -o /var/tmp/libvirt-restore/my-vm-restored
```

To stop replay at an intermediate run, read `runs.jsonl` from the chain dir, pick the `checkpoint` for the timestamp you want, and pass `--until`:

```sh
cat /var/tmp/libvirt-restore/my-vm-backup/runs.jsonl
sudo virtnbdrestore -a restore -i /var/tmp/libvirt-restore/my-vm-backup -o /var/tmp/libvirt-restore/my-vm-restored --until virtnbdbackup.3
```

After the restore completes, inspect the restored files, move the disk images to the intended libvirt storage location, define or update the VM using your site’s normal libvirt process, and boot it only after confirming the recovered disks, network identity, and any existing production instance will not conflict.
