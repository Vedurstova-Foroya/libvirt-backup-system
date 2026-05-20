"""Long-form help text rendered by the CLI argument parser.

The strings live in their own module so cli.py stays focused on argument-
parser wiring and the help text can be read end-to-end without the parser
scaffolding in the way. Every string here is rendered verbatim by
``argparse.RawDescriptionHelpFormatter``, so leading indentation and blank
lines matter.
"""

from __future__ import annotations

PROGRAM_DESCRIPTION = """\
libvirt-backup-system orchestrates virtnbdbackup against every running libvirt
VM on this host, writing per-month incremental chains under a configured
BACKUP_PATH. Backups are normally taken by the installed systemd timer
(default OnCalendar: *-*-* 02:30:00 UTC) so manual ``run`` invocations are
only needed for ad-hoc or recovery work.

Only running VMs are backed up. Offline VMs are logged as ``skipping vm
because it is offline`` and skipped; bring the VM up to back it up.

Configuration lives in /etc/libvirt-backup-system/libvirt-backup.env. Edit it
with ``sudoedit`` and then run ``start`` to refresh the systemd units."""


PROGRAM_EPILOG = """\
Common workflows:

  First install and activate the timer:
    sudo BACKUP_PATH=/mnt/qnap-backups libvirt-backup-system install
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
  Copy the first two whitespace-separated columns of any line printed by
  ``list-restore-points`` straight into this command. There is no rounding,
  no closest-match: TIMESTAMP is the exact per-run target.

How the chain is located:
  ``restore`` walks every host directory under BACKUP_PATH (not just the
  current HOST_ID) so a recovery host that mounted the backup tree can
  restore VMs that were taken on a different KVM host.

What action is chosen:
  OVERWRITE  Same host AND a local libvirt domain with VM_UUID exists.
             The current VM is force-shut-down (``virsh destroy``), undefined
             with ``--checkpoints-metadata`` (so virtnbdbackup's leftover
             libvirt checkpoints do not block the next step), and then
             redefined from the backup XML pointing at restored disks.
             Existing disk files are replaced. Refuses to proceed if the
             shutdown fails.

  TURNKEY    Anything else: cross-host recovery, or the local VM no longer
             exists. If the backup XML records file-backed disks in one
             directory, restored disks are written there; otherwise the backup
             is staged under
             /var/lib/libvirt-backup-system/restore/<uuid>-<timestamp>/.
             The restored XML is adjusted back to the original VM name and UUID
             before ``virsh define``. The recovered VM is one ``virsh start``
             away from booting.

What the underlying command runs:
  Both modes invoke
    virtnbdrestore -a restore -i <chain> -o <output> -u <checkpoint> --name <vm> -c -C <xml>
  with the checkpoint resolved by exact timestamp match against the chain's
  runs.jsonl. Legacy chains predating runs.jsonl omit ``-u`` and replay end-
  to-end. By default restore prints only summary success/error events; pass
  ``-v``/``--verbose`` to stream the full virtnbdrestore output.

Safety guarantees:
  * VM_BLACKLIST is ignored: blacklisting scopes to *taking* new backups, not
    to restoring from existing ones.
  * Poisoned chains (chains a previous run marked as having a half-written
    incremental) are refused outright; pick a different timestamp.
  * The staging directory is recreated on every restore so a leftover from
    an interrupted earlier restore cannot contaminate the current one.
  * Holds the same run-lock as ``run`` to avoid reading a chain directory
    that a concurrent backup is still writing into.

Example:
  sudo libvirt-backup-system list-restore-points | grep my-vm
  sudo libvirt-backup-system restore \\
       aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa 20260507T101112"""


INSTALL_HELP = "Install the wrapper, config file, package copy, and systemd units."
INSTALL_DESCRIPTION = """\
Install libvirt-backup-system: copy the package to /opt/libvirt-backup-system,
write the /usr/local/bin/libvirt-backup-system wrapper, drop the default
/etc/libvirt-backup-system/libvirt-backup.env (preserving an existing one),
render the systemd unit files, and install the fish completion script. The
timer is NOT enabled automatically — run ``check`` and then ``start`` after
editing the env file to activate it.

For a one-shot first install with BACKUP_PATH:

  sudo BACKUP_PATH=/mnt/qnap-backups libvirt-backup-system install

If the config file already exists the install-time environment is ignored,
so re-running ``install`` never overwrites operator edits."""


UNINSTALL_HELP = "Remove installed files. Config/state/logs/backups are kept unless --purge-* is passed."
UNINSTALL_DESCRIPTION = """\
Disable the timer, stop the service, and remove the installed wrapper, opt
directory, systemd unit files, and fish completion script. Config, state,
logs, and on-disk backups are preserved by default so an accidental uninstall
does not destroy data; use the --purge-* flags to remove them explicitly.

The on-disk backup tree under BACKUP_PATH is never touched by uninstall; that
has to be removed by hand once the operator is sure the backups are no longer
needed."""


CHECK_HELP = "Run preflight: validate config, binaries, paths, and free space."
CHECK_DESCRIPTION = """\
Validate the environment before a backup run: config keys are present and
typed correctly, required binaries (virsh, virtnbdbackup, virtnbdrestore,
qemu-img, df) are on PATH, libvirt is reachable, BACKUP_PATH is writable and
(when BACKUP_REQUIRE_NFS_MOUNT=true) is a mounted filesystem, the scratch
directory is writable, the NBD socket bind probe succeeds against a running
VM, and df reports enough free space to satisfy the estimated chain size for
all selected VMs.

``preflight`` is an alias of ``check``."""


DOCTOR_HELP = "Run the full preflight surface plus install/registration and last-run health."
DOCTOR_DESCRIPTION = """\
Superset of ``check``: runs the full preflight layer and then validates that
the wrapper, opt directory, and config file are in place; the three systemd
unit files match what a fresh install would render (catches drift after
editing the env file without re-running install); the timer is enabled and
active; and the most recent libvirt-backup-system.service run completed
cleanly.

Use ``check`` for the pre-run preflight only; use ``doctor`` when you also
want install/registration/last-run health."""


RUN_HELP = "Acquire the run lock, run preflight, back up every running VM."
RUN_DESCRIPTION = """\
Manual backup invocation. Acquires the run lock, runs ``check``, and then
backs up every selected running VM. Offline VMs are logged as
``skipping vm because it is offline`` and skipped. Each running VM builds a
per-month incremental chain: the first run of the calendar month is a
``-l full``, subsequent runs in the same month are ``-l inc`` appended to the
same chain directory.

Manual runs require the systemd timer to have been activated first with
``start`` — on a systemd host, ``run`` exits non-zero with
``backup service is not running`` instead of starting an ad-hoc backup if
the unit has not been installed and activated.

When BACKUP_CLEANUP_ON_RUN=true (the default) the run finishes by pruning
month directories older than BACKUP_RETENTION_MONTHS. Pruning failures never
roll back successful backups; the exit code is the worst of backup vs.
prune."""


START_HELP = "Install/refresh systemd units from the env file and activate the timer."
START_DESCRIPTION = """\
Render the systemd unit files from the current env file, reload systemd, and
enable + start libvirt-backup-system.timer. This activates the schedule
only — it does not run a backup immediately. Use ``start`` after ``install``
and after every edit to /etc/libvirt-backup-system/libvirt-backup.env that
changes BACKUP_PATH or SYSTEMD_ON_CALENDAR. Use ``run`` for a manual
backup."""


STATUS_HELP = "Print systemctl status for the installed timer and service."


LIST_VMS_HELP = "List selected VMs after VM_BLACKLIST is applied."
LIST_VMS_DESCRIPTION = """\
Print one row per VM that ``run`` would currently consider, after
VM_BLACKLIST (UUID-based) is applied. Default output is one
``<name>\\t<state>\\t<uuid>`` line per VM; ``--json`` emits a JSON array
suitable for piping into ``jq``. Pass ``--include-blacklisted`` to also list
VMs that are present in libvirt but currently filtered out by VM_BLACKLIST."""


VERIFY_HELP = "Run ``virtnbdrestore -a verify`` against discovered backup directories."
VERIFY_DESCRIPTION = """\
Replay every backup directory under BACKUP_PATH through
``virtnbdrestore -a verify`` to confirm the chain is internally consistent.
``--vm <name>`` restricts verification to one VM (current name or libvirt
UUID). VM_BLACKLIST is intentionally ignored: a VM that was added to the
blacklist may still have valid older backups that the operator wants to
verify."""


LIST_RESTORE_POINTS_HELP = "List every restorable backup run across all hosts and VMs."
LIST_RESTORE_POINTS_DESCRIPTION = """\
Walk BACKUP_PATH/<host>/<vm-uuid>/<yyyy-mm>/<chain>/ for every host directory
present under BACKUP_PATH (not just the current HOST_ID) and print one row
per restorable run. The first two columns are the VM_UUID and the per-run
TIMESTAMP so the operator can copy-paste that pair straight into ``restore``.

Modern chains (those with runs.jsonl) produce one row per recorded run;
legacy chains predating runs.jsonl produce a single chain-end row identified
by the chain directory name."""


RESTORE_HELP = "Restore a backup run identified by VM_UUID and TIMESTAMP."
