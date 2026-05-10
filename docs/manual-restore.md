# Manual restore process

This project does not provide a restore command. Restoring a VM is an operator-led recovery task because the correct target host, storage pool, VM definition, networking, and cutover process are site-specific.

Backups are stored as:

```text
BACKUP_PATH/<host-id>/<vm-name>/<yyyy-mm>/<timestamp>/
```

## Recovery outline

Pick the exact backup snapshot to recover:

```sh
SOURCE=/mnt/qnap-backups/myhost/my-vm/2026-05/20260517T023000Z
```

Copy it to local storage on the recovery host:

```sh
sudo mkdir -p /var/tmp/libvirt-restore/my-vm-backup
sudo rsync -aH --numeric-ids "$SOURCE"/ /var/tmp/libvirt-restore/my-vm-backup/
```

Verify the copied backup before restoring from it:

```sh
sudo virtnbdrestore -i /var/tmp/libvirt-restore/my-vm-backup -o verify
```

Restore into an empty staging directory:

```sh
sudo mkdir -p /var/tmp/libvirt-restore/my-vm-restored
sudo virtnbdrestore -i /var/tmp/libvirt-restore/my-vm-backup -o restore -D /var/tmp/libvirt-restore/my-vm-restored
```

After the restore completes, inspect the restored files, move the disk images to the intended libvirt storage location, define or update the VM using your site’s normal libvirt process, and boot it only after confirming the recovered disks, network identity, and any existing production instance will not conflict.
