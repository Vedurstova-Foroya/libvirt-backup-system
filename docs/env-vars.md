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

# Number of monthly backup directories to keep per VM.
# Set to -1 to keep all months (cleanup never prunes).
# 0 is rejected by preflight to avoid an unintentional "delete everything".
# BACKUP_RETENTION_MONTHS=12

# Extra free-space margin added to preflight's backup size estimate.
# SPACE_MARGIN_PERCENT=20

# Stopped VMs are copied once per month by default.
# Set true to copy stopped VMs on every run.
# INACTIVE_COPY_EVERY_RUN=false

# Per-VM backup size estimate used by preflight space checks, in GB.
# Used only as a fallback when disk introspection (virsh / qemu-img) fails.
# BACKUP_ESTIMATE_GB_PER_VM=1

# Multiplier applied to the sum of VM disk virtual sizes when estimating
# required backup space (accounts for incremental overhead and metadata).
# BACKUP_INCREMENTAL_MULTIPLIER=1.2

# Require preflight and run commands to execute as root.
# REQUIRE_ROOT=true

# Timeout for external commands, in seconds.
# COMMAND_TIMEOUT_SECONDS=86400
