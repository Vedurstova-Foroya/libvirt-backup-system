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
#   BACKUP_PATH/<host-id>/<vm-name>/<yyyy-mm>/<timestamp>/
BACKUP_PATH=

# Backup host folder name. Empty means "use this machine's short hostname".
# Keep this stable if the hostname might change.
# HOST_ID=

# VM names to skip. Separate with spaces or commas.
# VM_BLACKLIST=

# Add --compress to virtnbdbackup commands.
# BACKUP_COMPRESS=true

# systemd OnCalendar value used when the timer unit is installed.
# Re-run install or edit/reload the timer if this changes after install.
# SYSTEMD_ON_CALENDAR=*-*-* 02:30:00

# Require BACKUP_PATH to be a mounted filesystem, usually an NFS/QNAP mount.
# Set false when backing up to an intentionally local directory.
# BACKUP_REQUIRE_NFS_MOUNT=true

# Retention and cleanup are intentionally out of scope for this system: it only
# writes backups and never deletes them. There is no BACKUP_RETENTION_MONTHS
# variable, no cleanup subcommand, and no implicit retention. Manage retention
# externally (cron + find/rm, storage-side snapshot policy, NFS/QNAP appliance
# feature) and see docs/commands.md "Non-goals" before reintroducing any
# pruning behavior here.

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
# required backup space. Backups are full-per-run (no incremental chain); the
# name is historical. The multiplier accounts for compression overhead,
# metadata, and per-VM safety margin on top of the raw disk virtual size.
# BACKUP_INCREMENTAL_MULTIPLIER=1.2

# Require preflight and run commands to execute as root.
# REQUIRE_ROOT=true

# Timeout for external commands, in seconds.
# COMMAND_TIMEOUT_SECONDS=86400
