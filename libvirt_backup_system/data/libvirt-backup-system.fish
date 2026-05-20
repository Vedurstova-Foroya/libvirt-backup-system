# Fish completion for libvirt-backup-system.
#
# Installed automatically by `libvirt-backup-system install` to
#   /usr/share/fish/vendor_completions.d/libvirt-backup-system.fish
# Fish auto-loads files in that directory; no `source` line is required.
#
# Keep this file in sync with the argparse parser in
# libvirt_backup_system/cli.py. A unit test parses the .fish file and
# cross-checks it against build_parser() so the two stay aligned.

# Disable default file completion: the binary takes named subcommands, not
# file paths. Subcommand-specific blocks below re-enable -F (file path) for
# the few flags that accept paths.
complete -c libvirt-backup-system -f

set -l __lbs_subcommands install uninstall check preflight doctor run start status list-vms verify list-restore-points restore

# Top-level subcommand suggestions (only before any subcommand has been chosen).
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a install -d "Install wrapper, config, package, and systemd units"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a uninstall -d "Remove installed files (config/state/logs kept unless --purge-* is passed)"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a check -d "Run preflight: config, binaries, paths, free space"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a preflight -d "Alias of check"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a doctor -d "Full preflight + install/registration/last-run health"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a run -d "Acquire the run lock and back up every running VM"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a start -d "Refresh systemd units and activate the timer"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a status -d "systemctl status for the installed timer and service"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a list-vms -d "List selected VMs after VM_BLACKLIST is applied"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a verify -d "Run virtnbdrestore -a verify against discovered backups"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a list-restore-points -d "List every restorable backup run across all hosts and VMs"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a restore -d "Restore a backup run identified by VM_UUID and TIMESTAMP"

# Global flags (available at every position).
complete -c libvirt-backup-system -l config -r -F -d "Path to libvirt-backup.env"
complete -c libvirt-backup-system -l prefix -r -F -d "Root prefix for install/runtime paths"
complete -c libvirt-backup-system -s h -l help -d "Show help and exit"

# uninstall flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-config -d "Also remove /etc/libvirt-backup-system/libvirt-backup.env"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-state -d "Also remove /var/lib/libvirt-backup-system/"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-logs -d "Also remove /var/log/libvirt-backup-system/"

# list-vms flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from list-vms" -l json -d "Emit a JSON array instead of tab-separated rows"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from list-vms" -l include-blacklisted -d "Also list VMs filtered out by VM_BLACKLIST"

# verify flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from verify" -l vm -r -d "Restrict verification to one VM by name or UUID"

# --- Dynamic restore completion ---------------------------------------------
#
# `restore` takes (VM_UUID, TIMESTAMP) — both copy-pasted from the
# `list-restore-points` output by the operator. Querying the binary on every
# TAB shows the actual available restore points so the operator does not have
# to keep two terminals open.
#
# list-restore-points needs root to read /etc/libvirt-backup-system/ and the
# backup tree. Use ``sudo -n`` so completion never prompts for a password
# mid-TAB; if the sudo token has lapsed, fall back to a non-sudo invocation
# (that succeeds on hosts where the env file and BACKUP_PATH happen to be
# user-readable, and silently produces no rows otherwise). stderr is dropped
# so partial failures do not pollute the completion menu.

function __lbs_query_restore_points
    sudo -n libvirt-backup-system list-restore-points 2>/dev/null
    or libvirt-backup-system list-restore-points 2>/dev/null
end

# Number of positional args already typed after the `restore` subcommand.
# Returns 0 (suggest UUID), 1 (suggest TIMESTAMP), or -1 (not in a restore
# context). The literal-`restore` scan tolerates global flags (--config,
# --prefix) preceding the subcommand and the fish-builtin `sudo` prefix
# stripping.
function __lbs_restore_positional_count
    set -l tokens (commandline -opc)
    set -l found 0
    set -l count 0
    for token in $tokens
        if test $found = 1
            set count (math $count + 1)
        end
        if test "$token" = restore
            set found 1
        end
    end
    if test $found = 0
        echo -1
    else
        echo $count
    end
end

function __lbs_restore_uuids
    # Deduplicate by UUID so a VM with many restore points appears once in the
    # menu. The description shows the VM name and the count of restore points
    # so the operator can see at a glance which VMs have multiple snapshots;
    # the per-snapshot detail (full vs inc) surfaces on the second TAB once a
    # UUID is picked. Avoid putting "full"/"inc" here — the first row for any
    # UUID is always the chain full, which made it look like only full
    # backups existed when the menu actually lists VMs.
    __lbs_query_restore_points | awk 'NR > 1 { c[$1]++; if (!s[$1]++) n[$1] = $3 } END { for (u in c) printf "%s\t%s (%d backups)\n", u, n[u], c[u] }' | sort
end

function __lbs_restore_timestamps_for_uuid
    set -l tokens (commandline -opc)
    set -l found 0
    set -l uuid ""
    for token in $tokens
        if test $found = 1
            set uuid $token
            break
        end
        if test "$token" = restore
            set found 1
        end
    end
    if test -z "$uuid"
        return
    end
    # ``sort -r`` puts the most recent timestamp at the top of the menu so
    # the operator's typical "restore to the latest point" intent lands a
    # single arrow-down away. The description is just the kind ("full" /
    # "inc") so the operator can spot standalone-restorable points versus
    # chain-dependent ones.
    __lbs_query_restore_points | awk -v u="$uuid" 'NR > 1 && $1 == u {print $2"\t"$5}' | sort -r
end

complete -c libvirt-backup-system \
    -n '__fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 0' \
    -f -a '(__lbs_restore_uuids)'

complete -c libvirt-backup-system -k \
    -n '__fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 1' \
    -f -a '(__lbs_restore_timestamps_for_uuid)'

# Fish's stock sudo completion (in /usr/share/fish/completions/sudo.fish)
# dispatches via __fish_complete_subcommand, but its own registration on
# ``sudo`` does NOT carry ``-k``. So even though our libvirt-backup-system
# registration above has ``-k`` and our awk pipeline emits timestamps newest-
# first, fish re-sorts the candidates alphabetically once they bubble up
# through the sudo dispatcher — and ISO timestamps sort alphabetically
# ASCENDING, which is exactly the opposite of what operators want.
#
# Mirror the two restore-stage registrations directly under ``sudo`` (with
# ``-k`` on the timestamp stage) so our ordering survives sudo dispatch. The
# generic sudo dispatcher still fires too and produces the same candidates;
# fish dedups the union and the ``-k`` flag wins as long as a matching
# registration carries it.
complete -c sudo -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 0' \
    -a '(__lbs_restore_uuids)'

complete -c sudo -k -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 1' \
    -a '(__lbs_restore_timestamps_for_uuid)'
