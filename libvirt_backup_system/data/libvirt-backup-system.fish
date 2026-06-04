# Fish completion for libvirt-backup-system.
#
# Installed automatically by `libvirt-backup-system install` to
#   /usr/share/fish/vendor_completions.d/libvirt-backup-system.fish
# Fish auto-loads files in that directory; no `source` line is required.
#
# Keep this file in sync with the argparse parser in
# libvirt_backup_system/cli_parser.py. A unit test parses the .fish file and
# cross-checks it against build_parser() so the two stay aligned.

# Disable default file completion: the binary takes named subcommands, not
# file paths. Subcommand-specific blocks below re-enable -F (file path) for
# the few flags that accept paths.
complete -c libvirt-backup-system -f

set -l __lbs_subcommands install change-password uninstall check preflight doctor run start status list-vms verify list-restore-points du restore

# Top-level subcommand suggestions (only before any subcommand has been chosen).
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a install -d "Install wrapper, config, package, and systemd units"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a change-password -d "Rotate the shared kopia password on the local host"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a uninstall -d "Remove installed files (config/state/logs kept unless --purge-* is passed)"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a check -d "Run preflight: config, binaries, paths, free space"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a preflight -d "Alias of check"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a doctor -d "Full preflight + install/registration/last-run health"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a run -d "Acquire the run lock and back up every running VM"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a start -d "Refresh systemd units and activate schedules"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a status -d "systemctl status for installed timers and services"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a list-vms -d "List selected VMs after VM_BLACKLIST is applied"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a verify -d "Run kopia snapshot verify against discovered repos"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a list-restore-points -d "List every restorable backup run across all hosts and VMs"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a du -d "Show backup disk usage by host or VM"
complete -c libvirt-backup-system -n "not __fish_seen_subcommand_from $__lbs_subcommands" -a restore -d "Restore a backup run identified by VM_UUID and TIMESTAMP"

# Global flags (available at every position).
complete -c libvirt-backup-system -l config -r -F -d "Path to libvirt-backup.env"
complete -c libvirt-backup-system -l prefix -r -F -d "Root prefix for install/runtime paths"
complete -c libvirt-backup-system -s h -l help -d "Show help and exit"

# install flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password -r -d "Shared kopia repo password"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password-file -r -F -d "Path to a file holding the kopia password; '-' reads stdin"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password-env -r -d "Environment variable name holding the kopia password"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l acknowledge-password-loss -d "Acknowledge that losing the shared password makes backups unrecoverable"

# change-password flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from change-password" -l new-kopia-password -r -d "New shared kopia repo password"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from change-password" -l new-kopia-password-file -r -F -d "Path to a file holding the new kopia password; '-' reads stdin"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from change-password" -l new-kopia-password-env -r -d "Environment variable name holding the new kopia password"

# uninstall flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-config -d "Also remove /etc/libvirt-backup-system/libvirt-backup.env"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-state -d "Also remove /var/lib/libvirt-backup-system/"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from uninstall" -l purge-logs -d "Also remove /var/log/libvirt-backup-system/"

# list-vms flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from list-vms" -l json -d "Emit a JSON array instead of tab-separated rows"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from list-vms" -l include-blacklisted -d "Also list VMs filtered out by VM_BLACKLIST"

# list-restore-points flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from list-restore-points" -l json -d "Emit a JSON array instead of table rows"

# du flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from du" -l json -d "Emit a JSON object instead of table rows"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from du" -l host-id -r -f -d "Drill into one source host repo"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from du" -l vm-uuid -r -f -d "Drill into one VM across matching host repos"

# verify flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from verify" -l include-hosts -r -d "Comma-separated peer host_ids to verify in addition to the local repo"

# restore flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -s v -l verbose -d "Stream full restore output"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -l host-id -r -f -d "Disambiguate duplicate restore points by source host"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -l run-id -r -f -d "Disambiguate duplicate restore points by run ID"

# --- Dynamic restore completion ---------------------------------------------
#
# `restore` takes (VM_UUID, TIMESTAMP) — both copy-pasted from the
# `list-restore-points` output by the operator. Completion keeps a short-lived
# cache so TAB does not rescan Kopia on every keypress.
#
# list-restore-points needs root to read /etc/libvirt-backup-system/ and the
# backup tree. Use ``sudo -n`` so completion never prompts for a password
# mid-TAB; if the sudo token has lapsed, fall back to a non-sudo invocation
# (that succeeds on hosts where the env file and BACKUP_PATH happen to be
# user-readable, and silently produces no rows otherwise). stderr is dropped
# so partial failures do not pollute the completion menu.

function __lbs_query_restore_points_uncached
    sudo -n libvirt-backup-system list-restore-points 2>/dev/null
    or libvirt-backup-system list-restore-points 2>/dev/null
end

function __lbs_restore_cache_file
    set -l root
    if set -q XDG_CACHE_HOME; and test -n "$XDG_CACHE_HOME"
        set root "$XDG_CACHE_HOME"
    else if set -q HOME; and test -n "$HOME"
        set root "$HOME/.cache"
    else
        set root /tmp
    end
    echo "$root/libvirt-backup-system/restore-points.tsv"
end

function __lbs_refresh_restore_points_cache
    set -l cache (__lbs_restore_cache_file)
    set -l tmp "$cache."(date +%s).(random)
    command mkdir -p (dirname "$cache") 2>/dev/null; or return 1
    __lbs_query_restore_points_uncached >"$tmp"
    if test $status -eq 0; and test -s "$tmp"
        command mv -f "$tmp" "$cache"
    else
        command rm -f "$tmp"
        return 1
    end
end

function __lbs_restore_cache_is_fresh
    set -l cache "$argv[1]"
    test -f "$cache"; or return 1
    set -l mtime (command stat -c %Y "$cache" 2>/dev/null); or return 1
    test (math (command date +%s) - $mtime) -lt 5
end

function __lbs_query_restore_points
    set -l cache (__lbs_restore_cache_file)
    if test -f "$cache"
        if not __lbs_restore_cache_is_fresh "$cache"
            __lbs_refresh_restore_points_cache >/dev/null 2>/dev/null
        end
        command cat "$cache"
        return 0
    end
    __lbs_refresh_restore_points_cache >/dev/null 2>/dev/null; and command cat "$cache"
end

function __lbs_restore_is_option
    set -l token "$argv[1]"
    test "$token" = -v; or test "$token" = --verbose
    or test "$token" = --host-id; or test "$token" = --run-id
    or string match -q -- '--host-id=*' "$token"
    or string match -q -- '--run-id=*' "$token"
end

function __lbs_restore_option_takes_value
    set -l token "$argv[1]"
    test "$token" = --host-id; or test "$token" = --run-id
end

# Number of positional args already typed after the `restore` subcommand.
# Returns 0 (suggest UUID), 1 (suggest TIMESTAMP), or -1 (not in a restore
# context). The literal-`restore` scan tolerates global flags (--config,
# --prefix) preceding the subcommand and the fish-builtin `sudo` prefix
# stripping. Restore-local flags like --verbose do not count as positional
# args.
function __lbs_restore_positional_count
    set -l tokens (commandline -opc)
    set -l found 0
    set -l count 0
    set -l skip_next 0
    for token in $tokens
        if test $found = 1
            if test $skip_next = 1
                set skip_next 0
                continue
            end
            if not __lbs_restore_is_option "$token"
                set count (math $count + 1)
            else if __lbs_restore_option_takes_value "$token"
                set skip_next 1
            end
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
    # menu. The Kopia-era list-restore-points table is:
    # source-host-id vm-uuid timestamp run-id vm-name. The description shows
    # the VM name, first host seen, and restore point count for that UUID.
    __lbs_query_restore_points | awk 'function vmname(i, n) { n=$5; for (i=6; i<=NF; i++) n=n" "$i; return n } NR > 1 { c[$2]++; if (!s[$2]++) { h[$2]=$1; n[$2]=vmname() } } END { for (u in c) printf "%s\t%s - %s (%d restore points)\n", u, n[u], h[u], c[u] }' | sort
end

function __lbs_restore_timestamps_for_uuid
    set -l tokens (commandline -opc)
    set -l found 0
    set -l uuid ""
    set -l skip_next 0
    for token in $tokens
        if test $found = 1
            if test $skip_next = 1
                set skip_next 0
                continue
            end
            if not __lbs_restore_is_option "$token"
                set uuid $token
                break
            else if __lbs_restore_option_takes_value "$token"
                set skip_next 1
            end
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
    # single arrow-down away. The description shows source host and RUN_ID for
    # diagnostics without requiring the operator to keep the table visible.
    __lbs_query_restore_points | awk -v u="$uuid" 'function vmname(i, n) { n=$5; for (i=6; i<=NF; i++) n=n" "$i; return n } NR > 1 && $2 == u {print $3"\t"$1" "$4" "vmname()}' | sort -r
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
