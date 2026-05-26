"""Long-form help text rendered by the CLI argument parser.

The strings live in their own module so cli.py stays focused on argument-
parser wiring and the help text can be read end-to-end without the parser
scaffolding in the way. Every string here is rendered verbatim by
``argparse.RawDescriptionHelpFormatter``, so leading indentation and blank
lines matter.
"""

from __future__ import annotations

PROGRAM_DESCRIPTION = """\
libvirt-backup-system orchestrates kopia-backed backups of every running
libvirt VM on this host, writing snapshots into a per-host kopia repository
under ``BACKUP_PATH/<host-id>/kopia-repo/``. Backups are normally taken by
the installed systemd timer (default OnCalendar: *-*-* 02:30:00 UTC) so
manual ``run`` invocations are only needed for ad-hoc or recovery work.

Only running VMs are backed up. Offline VMs are logged as ``skipping vm
because it is offline`` and skipped; bring the VM up to back it up.

Configuration lives in /etc/libvirt-backup-system/libvirt-backup.env. Edit it
with ``sudoedit`` and then run ``start`` to refresh the systemd units."""


PROGRAM_EPILOG = """\
Common workflows:

  First install and activate the timer:
    sudo BACKUP_PATH=/mnt/qnap-backups libvirt-backup-system install \\
         --kopia-password-file=/root/kopia.pw
    sudoedit /etc/libvirt-backup-system/libvirt-backup.env
    sudo libvirt-backup-system check
    sudo libvirt-backup-system start

  Daily operation:
    sudo libvirt-backup-system status
    sudo libvirt-backup-system list-vms
    sudo libvirt-backup-system doctor

  Restore a single backup run:
    sudo libvirt-backup-system list-restore-points | grep my-vm
    sudo libvirt-backup-system restore <VM_UUID> <TIMESTAMP>

Run ``libvirt-backup-system <subcommand> --help`` for the full reference on any
subcommand. The ``restore`` help in particular documents the overwrite-vs-
turnkey decision, the staging directory layout, and the safety guarantees."""


RESTORE_DESCRIPTION = """\
Restore a single backup run identified by its (VM_UUID, TIMESTAMP) pair.

How to pick the arguments:
  Copy the ``vm-uuid`` and ``timestamp`` columns of any line printed by
  ``list-restore-points`` straight into this command. There is no rounding,
  no closest-match: TIMESTAMP is the exact per-run target.

How the snapshot is located:
  ``restore`` walks every per-host kopia repo discovered under
  ``BACKUP_PATH/<host>/kopia-repo/`` (not just the current HOST_ID) so a
  recovery host that mounted the backup tree can restore VMs that were
  taken on a different KVM host. The per-run ``kind:meta`` snapshot is
  matched by its ``vm-uuid`` and start-time tags.

What action is chosen:
  OVERWRITE  Same host AND a local libvirt domain with VM_UUID exists.
             The current VM is force-shut-down (``virsh destroy``),
             undefined with ``--checkpoints-metadata`` (to clear any
             leftover libvirt checkpoints), and then redefined from the
             backup XML pointing at restored disks. Existing disk files
             are replaced. Refuses to proceed if the shutdown fails.

  TURNKEY    Anything else: cross-host recovery, or the local VM no longer
             exists. Restored disks are written under
             /var/lib/libvirt-backup-system/restore/<uuid>-<timestamp>/ and
             the domain XML is rewritten so ``<source>`` elements point at
             the restored qcow2 files. The recovered VM is one
             ``virsh start`` away from booting.

What the underlying command runs:
  ``restore`` materializes the per-run manifest by streaming the meta
  snapshot via ``kopia snapshot restore``, then for each disk in the
  manifest pipes ``kopia snapshot restore <snap-id>/<file> -`` into
  ``qemu-img convert -f raw -O qcow2 -S 4096 -`` to produce a sparse qcow2
  at the chosen destination. By default restore prints only summary
  success/error events; pass ``-v``/``--verbose`` to log each restored
  disk path.

Safety guarantees:
  * VM_BLACKLIST is ignored: blacklisting scopes to *taking* new backups, not
    to restoring from existing ones.
  * The staging directory is recreated on every restore so a leftover from
    an interrupted earlier restore cannot contaminate the current one.
  * Holds the same run-lock as ``run`` to avoid reading a repo state that
    a concurrent backup is still writing into.

Example:
  sudo libvirt-backup-system list-restore-points | grep my-vm
  sudo libvirt-backup-system restore \\
       aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 20260507T101112"""


INSTALL_HELP = "Install the wrapper, config file, package copy, and systemd units."
INSTALL_DESCRIPTION = """\
Install libvirt-backup-system: copy the package to /opt/libvirt-backup-system,
write the /usr/local/bin/libvirt-backup-system wrapper, drop the default
/etc/libvirt-backup-system/libvirt-backup.env (preserving an existing one),
render the systemd unit files, lay down the shared kopia password file at
KOPIA_PASSWORD_FILE, and install the fish completion script. The timer is
NOT enabled automatically -- run ``check`` and then ``start`` after editing
the env file to activate it.

For a one-shot first install with BACKUP_PATH and a kopia password:

  sudo BACKUP_PATH=/mnt/qnap-backups libvirt-backup-system install \\
       --kopia-password-file=/root/kopia.pw \\
       --acknowledge-password-loss

The same shared kopia password must be used on every participating host so
each peer can read every peer's repo. If the config file already exists the
install-time environment is ignored, so re-running ``install`` never
overwrites operator edits."""


UNINSTALL_HELP = "Remove installed files. Config/state/logs/backups are kept unless --purge-* is passed."
UNINSTALL_DESCRIPTION = """\
Disable the timer, stop the service, and remove the installed wrapper, opt
directory, systemd unit files, and fish completion script. Config, state,
logs, and on-disk backups are preserved by default so an accidental uninstall
does not destroy data; use the --purge-* flags to remove them explicitly.

The on-disk kopia repo under BACKUP_PATH is never touched by uninstall; that
has to be removed by hand once the operator is sure the backups are no longer
needed."""


CHECK_HELP = "Run preflight: validate config, binaries, paths, and free space."
CHECK_DESCRIPTION = """\
Validate the environment before a backup run: config keys are present and
typed correctly, required binaries (virsh, qemu-nbd, nbdcopy, qemu-img, df,
kopia) are on PATH, libvirt is reachable, BACKUP_PATH is writable and
(when BACKUP_REQUIRE_NFS_MOUNT=true) is a mounted filesystem, the scratch
directory is writable, KOPIA_PASSWORD_FILE exists with mode 600 and (under
root) is owned by root, and df reports enough free space to satisfy the
estimated repo growth for all selected VMs.

``preflight`` is an alias of ``check``."""


DOCTOR_HELP = "Run the full preflight surface plus install/registration and last-run health."
DOCTOR_DESCRIPTION = """\
Superset of ``check``: runs the full preflight layer and then validates that
the wrapper, opt directory, and config file are in place; the systemd unit
files match what a fresh install would render (catches drift after editing
the env file without re-running install); the timer is enabled and active;
the local kopia repo is connected and accessible; local kopia maintenance
and verify dry-runs pass; peer repos are reachable read-only with the shared
password; and the most recent libvirt-backup-system.service run completed
cleanly.

Use ``check`` for the pre-run preflight only; use ``doctor`` when you also
want install/registration/last-run health."""


RUN_HELP = "Acquire the run lock, run preflight, back up every running VM."
RUN_DESCRIPTION = """\
Manual backup invocation. Acquires the run lock, runs ``check``, and then
backs up every selected running VM. Offline VMs are logged as
``skipping vm because it is offline`` and skipped. Each running VM is
streamed disk-by-disk into the local kopia repo via
``kopia snapshot create --stdin-file`` and tagged with
``kind:disk,run-id:<uuid>,disk:<target>,vm-uuid:<uuid>``. A per-run
``kind:meta`` snapshot carries the manifest with the domain XML and disk
listing so restore can reconstruct the VM without re-asking libvirt.

Manual runs require the systemd timer to have been activated first with
``start`` -- on a systemd host, ``run`` exits non-zero with
``backup service is not running`` instead of starting an ad-hoc backup if
the unit has not been installed and activated.

Retention is driven by the kopia global policy (KEEP_LATEST / KEEP_HOURLY /
KEEP_DAILY / KEEP_WEEKLY / KEEP_MONTHLY / KEEP_ANNUAL) applied at install
time and refreshed on ``start``; old snapshots are reaped by the periodic
``kopia maintenance`` units rather than at the tail of ``run``."""


START_HELP = "Install/refresh systemd units from the env file and activate the timer."
START_DESCRIPTION = """\
Render the systemd unit files from the current env file, reload systemd, and
enable + start libvirt-backup-system.timer (and the kopia maintenance and
verify timers). This activates the schedule only -- it does not run a backup
immediately. Use ``start`` after ``install`` and after every edit to
/etc/libvirt-backup-system/libvirt-backup.env that changes BACKUP_PATH or
SYSTEMD_ON_CALENDAR. Use ``run`` for a manual backup."""


STATUS_HELP = "Print systemctl status for the installed timer and service."


LIST_VMS_HELP = "List selected VMs after VM_BLACKLIST is applied."
LIST_VMS_DESCRIPTION = """\
Print one row per VM that ``run`` would currently consider, after
VM_BLACKLIST (UUID-based) is applied. Default output is one
``<name>\\t<state>\\t<uuid>`` line per VM; ``--json`` emits a JSON array
suitable for piping into ``jq``. Pass ``--include-blacklisted`` to also list
VMs that are present in libvirt but currently filtered out by VM_BLACKLIST."""


VERIFY_HELP = "Run ``kopia snapshot verify`` against discovered kopia repos."
VERIFY_DESCRIPTION = """\
Replay every snapshot in this host's local kopia repo through
``kopia snapshot verify --max-failures=0 --verify-files-percent=...`` to
confirm the repo is internally consistent. Pass
``--include-hosts=HOST_ID[,HOST_ID...]`` to additionally verify the named
peer repos discovered under ``BACKUP_PATH/<host>/kopia-repo/``; without the
flag only the local repo is checked. VM_BLACKLIST is intentionally ignored:
a VM that was added to the blacklist may still have valid older snapshots
that the operator wants to verify."""


LIST_RESTORE_POINTS_HELP = "List every restorable backup run across all hosts and VMs."
LIST_RESTORE_POINTS_DESCRIPTION = """\
Connect read-only to every per-host kopia repo discovered under
``BACKUP_PATH/<host>/kopia-repo/`` (including the local repo) and list every
``kind:meta`` snapshot -- one per backup run. Copy the VM_UUID and per-run
TIMESTAMP columns straight into ``restore``. Rows include source host, VM
name, and RUN_ID, and are grouped by source host so backups taken on a
different KVM host are visible alongside the local ones."""


RESTORE_HELP = "Restore a backup run identified by VM_UUID and TIMESTAMP."


CHANGE_PASSWORD_HELP = "Rotate the shared kopia password on the local host."
CHANGE_PASSWORD_DESCRIPTION = """\
Rotate the kopia repo password the local host writes to. The same shared
password lives on every participating host: run this command (with the same
new value) on each host independently. Order does not matter; each host
rotates its own local repo and password file.

Pick one of:
  --new-kopia-password=VALUE         password on the command line (visible to ps/journald)
  --new-kopia-password-file=PATH     read from file; '-' means stdin
  --new-kopia-password-env=VAR       read from the named environment variable

Behavior:
  1. Validate the current password file decrypts the local repo.
  2. ``kopia repository change-password`` rewraps the master key.
  3. Atomically replace the password file with the new value.

If step 3 fails after step 2 succeeds, the repo decrypts only with the new
password but the file still holds the old one. The log line names both
values; restore the new value into the file manually and re-run
``doctor``."""


KOPIA_PASSTHROUGH_HELP = "Run a raw ``kopia`` command against a managed repo (advanced)."
KOPIA_PASSTHROUGH_DESCRIPTION = """\
Hidden escape hatch for ad-hoc ``kopia`` invocations against a repo this
tool already manages. The wrapper resolves the correct
``--config-file=...`` and KOPIA_PASSWORD_FILE for you and then execs the
operator's ``kopia`` arguments verbatim.

By default the local host's repo connection-config is used:
  sudo libvirt-backup-system kopia-passthrough -- snapshot list

To target a peer repo discovered under ``BACKUP_PATH/<host-id>/kopia-repo/``
pass ``--host-id=<id>``:
  sudo libvirt-backup-system kopia-passthrough --host-id=other-kvm -- \\
       snapshot list --tags=kind:meta

Use a literal ``--`` between this command's flags and the kopia argv tail to
keep kopia's own ``--flags`` from being captured by argparse. The kopia
process inherits this command's stdin/stdout/stderr; its exit code is the
wrapper's exit code."""
