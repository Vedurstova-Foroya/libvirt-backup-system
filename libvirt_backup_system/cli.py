from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback

from . import cli_help
from .backup import run_backups
from .config import Config
from .doctor import doctor
from .installer import install, uninstall
from .list_restore_points import list_restore_points
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .preflight import check, validate_config
from .restore import restore
from .shell import configure_default_timeout
from .systemd_start import start
from .systemd_units import dispatch_via_systemd, status
from .verify import verify
from .vms import list_vms


def _add_subparser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
    name: str,
    *,
    help_text: str,
    description: str | None = None,
    aliases: list[str] | None = None,
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help_text,
        description=description or help_text,
        aliases=aliases or [],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="libvirt-backup-system",
        description=cli_help.PROGRAM_DESCRIPTION,
        epilog=cli_help.PROGRAM_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        help=(
            "Path to libvirt-backup.env. Supplying this flag forces ``run``/``check`` to "
            "execute in-process (the installed systemd unit bakes in a fixed config path, "
            "so honoring a different path means skipping systemd dispatch)."
        ),
    )
    parser.add_argument(
        "--prefix",
        metavar="DIR",
        help=(
            "Root prefix for every install/runtime path. Defaults to / on production "
            "hosts and to a per-test tmpdir under the unit suite. Set this when you want "
            "to install into a sandbox instead of the real filesystem."
        ),
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        title="subcommands",
        metavar="<subcommand>",
    )

    _add_subparser(sub, "install", help_text=cli_help.INSTALL_HELP, description=cli_help.INSTALL_DESCRIPTION)

    uninstall_parser = _add_subparser(
        sub, "uninstall", help_text=cli_help.UNINSTALL_HELP, description=cli_help.UNINSTALL_DESCRIPTION
    )
    uninstall_parser.add_argument(
        "--purge-config", action="store_true", help="Also remove /etc/libvirt-backup-system/libvirt-backup.env."
    )
    uninstall_parser.add_argument(
        "--purge-state", action="store_true", help="Also remove /var/lib/libvirt-backup-system/ (lock, host-id stamp)."
    )
    uninstall_parser.add_argument(
        "--purge-logs", action="store_true", help="Also remove /var/log/libvirt-backup-system/."
    )

    _add_subparser(
        sub, "check", help_text=cli_help.CHECK_HELP, description=cli_help.CHECK_DESCRIPTION, aliases=["preflight"]
    )
    _add_subparser(sub, "doctor", help_text=cli_help.DOCTOR_HELP, description=cli_help.DOCTOR_DESCRIPTION)
    _add_subparser(sub, "run", help_text=cli_help.RUN_HELP, description=cli_help.RUN_DESCRIPTION)
    _add_subparser(sub, "start", help_text=cli_help.START_HELP, description=cli_help.START_DESCRIPTION)
    _add_subparser(sub, "status", help_text=cli_help.STATUS_HELP)

    list_parser = _add_subparser(
        sub, "list-vms", help_text=cli_help.LIST_VMS_HELP, description=cli_help.LIST_VMS_DESCRIPTION
    )
    list_parser.add_argument("--json", action="store_true", help="Emit JSON array instead of tab-separated rows.")
    list_parser.add_argument(
        "--include-blacklisted", action="store_true", help="Also list VMs filtered out by VM_BLACKLIST."
    )

    verify_parser = _add_subparser(
        sub, "verify", help_text=cli_help.VERIFY_HELP, description=cli_help.VERIFY_DESCRIPTION
    )
    verify_parser.add_argument(
        "--include-hosts",
        metavar="HOST_ID[,HOST_ID...]",
        help="Comma-separated peer host_ids whose repos to verify in addition to the local repo.",
    )

    _add_subparser(
        sub,
        "list-restore-points",
        help_text=cli_help.LIST_RESTORE_POINTS_HELP,
        description=cli_help.LIST_RESTORE_POINTS_DESCRIPTION,
    )

    restore_parser = _add_subparser(
        sub, "restore", help_text=cli_help.RESTORE_HELP, description=cli_help.RESTORE_DESCRIPTION
    )
    restore_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream full virtnbdrestore output instead of only summary success/error events.",
    )
    restore_parser.add_argument(
        "vm_uuid",
        metavar="VM_UUID",
        help="VM libvirt UUID copied verbatim from the first column of list-restore-points output.",
    )
    restore_parser.add_argument(
        "timestamp",
        metavar="TIMESTAMP",
        help=(
            "Per-run timestamp (YYYYMMDDTHHMMSS) copied verbatim from the second column of "
            "list-restore-points output. Exact match against the chain's runs.jsonl."
        ),
    )

    return parser


def _run_command(config: Config) -> int:
    # Acquire the run lock before preflight: the new kopia-backed pipeline
    # spawns qemu-nbd against a running VM, so the lock keeps a concurrent
    # ad-hoc run from competing for the per-VM external snapshot.
    try:
        with acquire_run_lock(config):
            preflight_code = check(config, lock_held=True)
            if preflight_code != 0:
                return preflight_code
            return run_backups(config)
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _restore_command(config: Config, args: argparse.Namespace) -> int:
    config_code = validate_config(config)
    if config_code != 0:
        return config_code
    try:
        with acquire_run_lock(config):
            return restore(config, args.vm_uuid, args.timestamp, verbose=args.verbose)
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _list_restore_points_command(config: Config) -> int:
    config_code = validate_config(config)
    if config_code != 0:
        return config_code
    return list_restore_points(config)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            return install(args.prefix, config_path=args.config)
        if args.command == "start":
            return start(args.prefix, config_path=args.config)
        if args.command == "status":
            return status(args.prefix)
        if args.command == "uninstall":
            return uninstall(
                args.prefix,
                config_path=args.config,
                purge_config=args.purge_config,
                purge_state=args.purge_state,
                purge_logs=args.purge_logs,
            )

        # Route ``run``/``check`` through the installed systemd unit so the
        # operator's ad-hoc invocation runs in the same environment the
        # scheduled timer will use. ``dispatch_via_systemd`` returns ``None``
        # when dispatch is not appropriate (no INVOCATION_ID, --prefix or
        # --config overrides, systemd missing, unit not installed), in which
        # case we fall through and run the subcommand in-process.
        if args.command in {"check", "preflight", "run"}:
            mapped = "check" if args.command in {"check", "preflight"} else "run"
            dispatched = dispatch_via_systemd(mapped, prefix=args.prefix, config_path=args.config)
            if dispatched is not None:
                return dispatched

        if args.command == "list-vms" and args.json:
            with contextlib.redirect_stdout(sys.stderr):
                config = Config.load(config_path=args.config, prefix=args.prefix)
        else:
            config = Config.load(config_path=args.config, prefix=args.prefix)
        try:
            configure_default_timeout(config.get("COMMAND_TIMEOUT_SECONDS"))
        except ValueError as exc:
            event("error", "config validation failed", reason=str(exc))
            return 1
        if args.command in {"check", "preflight"}:
            return check(config)
        if args.command == "doctor":
            return doctor(config)
        if args.command == "run":
            return _run_command(config)
        if args.command == "list-vms":
            config_code = validate_config(config)
            if config_code != 0:
                return config_code
            vms = list_vms(config, include_blacklisted=args.include_blacklisted)
            if args.json:
                print(
                    json.dumps(
                        [{"name": vm.name, "uuid": vm.uuid, "state": vm.state, "running": vm.running} for vm in vms]
                    )
                )
            else:
                for vm in vms:
                    print(f"{vm.name}\t{vm.state}\t{vm.uuid}")
            return 0
        if args.command == "verify":
            config_code = validate_config(config)
            if config_code != 0:
                return config_code
            # Hold the same run-lock as ``run``: a concurrent run can expose a
            # half-written backup directory and produce a confusing
            # virtnbdrestore error. The lock surfaces "another run in
            # progress" instead.
            try:
                with acquire_run_lock(config):
                    include_hosts = (
                        [item.strip() for item in args.include_hosts.split(",") if item.strip()]
                        if args.include_hosts
                        else None
                    )
                    return verify(config, include_hosts=include_hosts)
            except LockBusyError as exc:
                event("error", "another run in progress", lock_path=str(exc.path))
                return 1
        if args.command == "restore":
            return _restore_command(config, args)
        if args.command == "list-restore-points":
            return _list_restore_points_command(config)
    except KeyboardInterrupt:
        event("error", "interrupted")
        return 130
    except Exception as exc:
        # The JSON record itself stays on one line (json.dumps escapes newlines
        # to "\n"), but those literal escape sequences still bloat the line
        # length and hide useful context behind scrolling. Collapse the
        # traceback to a single readable line so operators can grep one line
        # per event without losing the frame chain.
        flat_traceback = " | ".join(line.strip() for line in traceback.format_exc().splitlines() if line.strip())
        event("error", "fatal error", error=str(exc), traceback=flat_traceback)
        return 1

    parser.print_help(sys.stderr)
    return 2
