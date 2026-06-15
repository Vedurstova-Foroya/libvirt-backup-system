# Fish completion for libvirt-backup-system.
complete -c libvirt-backup-system -f

set -g __lbs_subcommands install add-node show-token change-password uninstall check preflight doctor run backup start status log logs list-vms verify list-restore-points du restore

function __lbs_no_subcommand_seen
    for token in (commandline -opc)
        contains -- $token $__lbs_subcommands; and return 1
    end
    return 0
end

# Top-level subcommand suggestions (only before any subcommand has been chosen).
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a install -d "Install wrapper, config, package, and systemd units"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a add-node -d "Print a pasteable install command for joining another host"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a show-token -d "Print the raw shared token from the secure password file"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a change-password -d "Rotate the shared kopia token on the local host"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a uninstall -d "Remove installed files (config/state/logs kept unless --purge-* is passed)"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a check -d "Run preflight: config, binaries, paths, free space"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a preflight -d "Alias of check"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a doctor -d "Full preflight + install/registration/last-run health"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a run -d "Acquire the run lock and back up every running VM"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a backup -d "Alias of run"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a start -d "Refresh systemd units and activate schedules"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a status -d "systemctl status for installed timers and services"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a log -d "Show backup logs from the journal (tail; -f to stream)"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a logs -d "Alias of log"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a list-vms -d "List selected VMs after VM_BLACKLIST is applied"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a verify -d "Run kopia snapshot verify against discovered repos"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a list-restore-points -d "List every restorable backup run across all hosts and VMs"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a du -d "Show backup disk usage by host or VM"
complete -c libvirt-backup-system -n "__lbs_no_subcommand_seen" -a restore -d "Restore a backup run identified by VM_UUID and TIMESTAMP"

# Global flags (available at every position).
complete -c libvirt-backup-system -l config -r -F -d "Path to libvirt-backup.env"
complete -c libvirt-backup-system -l prefix -r -F -d "Root prefix for install/runtime paths"
complete -c libvirt-backup-system -s h -l help -d "Show help and exit"

# install flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password -r -d "Explicit shared kopia token"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password-file -r -F -d "Path to a file holding the kopia password; '-' reads stdin"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l kopia-password-env -r -d "Environment variable name holding the kopia password"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from install" -l acknowledge-password-loss -d "Acknowledge that losing the shared token makes backups unrecoverable"

# change-password flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from change-password" -l new-kopia-password -r -d "New shared kopia token"
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

# log flags.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from log logs" -s f -l follow -d "Stream new log lines as they are written (like docker logs -f)"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from log logs" -s n -l lines -r -d "Recent lines to show before following (integer or 'all')"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from log logs" -f -a "run check maintenance maintenance-full verify all" -d "Unit journal to show"

# verify flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from verify" -l include-hosts -r -d "Comma-separated peer host_ids to verify in addition to the local repo"

# restore flag.
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -s v -l verbose -d "Stream full restore output"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -l host-id -r -f -d "Disambiguate duplicate restore points by source host"
complete -c libvirt-backup-system -n "__fish_seen_subcommand_from restore" -l run-id -r -f -d "Disambiguate duplicate restore points by run ID"

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

function __lbs_du_host_ids
    __lbs_query_restore_points | awk 'NR > 1 { c[$1]++ } END { for (h in c) printf "%s\t%d restore points\n", h, c[h] }' | sort
end

function __lbs_is_uuid
    string match -rq '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' -- "$argv[1]"
end

function __lbs_du_positional_tokens
    set -l found 0
    for token in (commandline -opc)
        if test $found = 1
            string match -q -- '-*' "$token"; and continue
            echo $token
        end
        if test "$token" = du
            set found 1
        end
    end
end

function __lbs_du_positional_count
    count (__lbs_du_positional_tokens)
end

function __lbs_du_first_positional
    set -l tokens (__lbs_du_positional_tokens)
    test (count $tokens) -gt 0; and echo $tokens[1]
end

function __lbs_du_first_args
    __lbs_du_host_ids
    __lbs_restore_uuids
end

function __lbs_du_second_args
    set -l host (__lbs_du_first_positional)
    test -n "$host"; or return
    __lbs_is_uuid "$host"; and return
    __lbs_query_restore_points | awk -v h="$host" 'function vmname(i, n) { n=$6; for (i=7; i<=NF; i++) n=n" "$i; return n } NR > 1 && $1 == h { c[$2]++; if (!s[$2]++) n[$2]=vmname() } END { for (u in c) printf "%s\t%s (%d restore points)\n", u, n[u], c[u] }' | sort
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
    # source-host-id vm-uuid timestamp run-id consistency vm-name. The description shows
    # the VM name, first host seen, and restore point count for that UUID.
    __lbs_query_restore_points | awk 'function vmname(i, n) { n=$6; for (i=7; i<=NF; i++) n=n" "$i; return n } NR > 1 { c[$2]++; if (!s[$2]++) { h[$2]=$1; n[$2]=vmname() } } END { for (u in c) printf "%s\t%s - %s (%d restore points)\n", u, n[u], h[u], c[u] }' | sort
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
    __lbs_query_restore_points | awk -v u="$uuid" 'function vmname(i, n) { n=$6; for (i=7; i<=NF; i++) n=n" "$i; return n } NR > 1 && $2 == u {print $3"\t"$1" "$4" "vmname()}' | sort -r
end

complete -c libvirt-backup-system \
    -n '__fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 0' \
    -f -a '(__lbs_restore_uuids)'

complete -c libvirt-backup-system -k \
    -n '__fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 1' \
    -f -a '(__lbs_restore_timestamps_for_uuid)'

complete -c sudo -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 0' \
    -a '(__lbs_restore_uuids)'

complete -c sudo -k -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from restore; and test (__lbs_restore_positional_count) = 1' \
    -a '(__lbs_restore_timestamps_for_uuid)'

complete -c libvirt-backup-system \
    -n '__fish_seen_subcommand_from du; and test (__lbs_du_positional_count) = 0' \
    -f -a '(__lbs_du_first_args)'

complete -c libvirt-backup-system \
    -n '__fish_seen_subcommand_from du; and test (__lbs_du_positional_count) = 1' \
    -f -a '(__lbs_du_second_args)'

complete -c sudo -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from du; and test (__lbs_du_positional_count) = 0' \
    -a '(__lbs_du_first_args)'

complete -c sudo -x \
    -n '__fish_seen_subcommand_from libvirt-backup-system; and __fish_seen_subcommand_from du; and test (__lbs_du_positional_count) = 1' \
    -a '(__lbs_du_second_args)'
