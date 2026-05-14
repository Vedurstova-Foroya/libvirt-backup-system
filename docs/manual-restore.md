# Manual restore process

Most operators should use the built-in `restore` subcommand:

```sh
sudo libvirt-backup-system restore --vm my-vm --output /var/tmp/restore/my-vm
```

That command picks the latest month and latest chain, holds the same run-lock as `run`, and invokes `virtnbdrestore -a restore -i <chain-dir> -o <output>`. Pass `--at <YYYY-MM-DD>` (or any of the accepted timestamp forms documented in [Command reference](commands.md#restore)) to select the chain whose start time is at-or-before the target.

This page covers the manual procedure for situations where the source backup must first be staged onto local storage (e.g. NFS read-only, off-host recovery), or where the operator needs full control over each step.

Backups are stored as:

```text
BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/
```

The `<chain-id>` is the timestamp of the first backup in the monthly incremental chain. Running VMs accumulate per-run incrementals into the same chain directory; each run also appends its `{ts, checkpoint}` entry to `runs.jsonl` inside the chain dir. To restore a specific intermediate run by hand, pass `virtnbdrestore --until <checkpoint>` with the matching name; without `--until` the whole chain replays.

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

Verify the copied backup before restoring from it (`-a` selects the action; `-o` is the required output path even for verify):

```sh
sudo virtnbdrestore -a verify -i /var/tmp/libvirt-restore/my-vm-backup -o /var/tmp/libvirt-restore/my-vm-backup
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
