# libvirt-backup-system environment file
#
# Installed path:
#   /etc/libvirt-backup-system/libvirt-backup.env
#
# Values in the real process environment override values in this file.
# Booleans accept (case-insensitive): 1, true, yes, on as true; 0, false, no, off as false.
# Any other value is rejected by preflight rather than silently coerced.

# Libvirt connection used by virsh for VM discovery and state checks.
# LIBVIRT_URI=qemu:///system

# Backup root. Backups are written as:
#   BACKUP_PATH/<host-id>/<vm-uuid>/<yyyy-mm>/<chain-id>/
# Running VMs use monthly incremental chains: the first run each calendar month
# is a full, later runs in the same month are incrementals into the same
# chain-id directory. Inactive (shut-off) VMs use ``-l copy`` and a per-month
# .inactive-copy-complete marker.
BACKUP_PATH=

# Backup host folder name. Empty means "use this machine's short hostname".
# Keep this stable if the hostname might change.
# HOST_ID=

# VM names to skip. Separate with spaces or commas.
# VM_BLACKLIST=

# Add --compress to virtnbdbackup commands.
# BACKUP_COMPRESS=true

# systemd OnCalendar value used when the timer unit is installed.
# Run start after changing this so the timer is refreshed and reloaded.
# SYSTEMD_ON_CALENDAR=*-*-* 02:30:00

# Require BACKUP_PATH to be a mounted filesystem, usually an NFS/QNAP mount.
# Set false when backing up to an intentionally local directory.
# BACKUP_REQUIRE_NFS_MOUNT=true

# Number of most-recent calendar months of backups to keep per VM. 0 disables
# pruning entirely; the default of 12 retains roughly one year. Retention is
# applied at the end of every successful run when BACKUP_CLEANUP_ON_RUN is true.
#
# Caveat for frequent libvirt XML edits: pruning is per *month* directory, not
# per chain. A mid-month fingerprint change (disk added, NIC swapped, ...)
# starts a fresh chain directory alongside the old one inside the same
# YYYY-MM/ folder. Both chains survive until the whole month falls out of the
# retention window, so frequent XML edits can accumulate intra-month chain
# dirs that stick around for the full retention horizon. This is a deliberate
# tradeoff to keep retention reasoning at month granularity; size your backup
# capacity accordingly if you expect many fingerprint changes per month.
# BACKUP_RETENTION_MONTHS=12

# Run the monthly retention pass at the end of every successful ``run``.
# Disable to manage retention out-of-band; pruning failures never roll back
# successful backups (the run returns the worst of backup vs. prune codes).
# BACKUP_CLEANUP_ON_RUN=true

# Extra free-space margin added to preflight's backup size estimate.
# SPACE_MARGIN_PERCENT=20

# Stopped VMs are copied once per month by default.
# Set true to copy stopped VMs on every run.
#
# Caveat for block-device-backed inactive VMs: when an inactive VM's disks are
# backed by raw block devices (LVM logical volumes, iSCSI LUNs, RBD images
# mapped as block devices, ...), the once-per-month fast path is not taken.
# The block-device inode mtime is rarely rewritten when its contents change, so
# the freshness check treats any block-backed disk as "possibly modified" and
# forces a recopy on every run, regardless of INACTIVE_COPY_EVERY_RUN. This is
# a deliberate correctness choice (the alternative is risking a stale copy);
# operators with block-backed inactive VMs should expect every-run copies for
# those VMs and size their backup window accordingly.
# INACTIVE_COPY_EVERY_RUN=false

# Per-VM backup size estimate used by preflight space checks, in GB.
# Used only as a fallback when disk introspection (virsh / qemu-img) fails.
# BACKUP_ESTIMATE_GB_PER_VM=1

# Multiplier applied to the sum of VM disk virtual sizes when estimating
# required backup space. Running VMs build a monthly incremental chain (one
# full + per-run increments); the multiplier accounts for compression
# overhead, metadata, and per-VM safety margin on top of the raw disk virtual
# size, sized for the worst-case full + full chain repopulation.
# BACKUP_INCREMENTAL_MULTIPLIER=1.2

# Require preflight and run commands to execute as root.
# REQUIRE_ROOT=true

# Timeout for external commands, in seconds.
# COMMAND_TIMEOUT_SECONDS=86400
