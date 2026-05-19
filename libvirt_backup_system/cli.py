from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback

from .backup import current_month, run_backups, verify
from .config import Config
from .doctor import doctor
from .installer import install, uninstall
from .list_restore_points import list_restore_points
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .preflight import check, validate_config
from .restore import restore
from .retention import prune_old_months
from .shell import configure_default_timeout
from .systemd_start import start
from .systemd_units import dispatch_via_systemd, status
from .vms import list_vms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libvirt-backup-system")
    parser.add_argument(
        "--config",
        help=(
            "Path to libvirt-backup.env. Supplying this flag forces ``run``/"
            "``check`` to execute in-process (the installed systemd unit "
            "bakes in a fixed config path, so honoring a different path means "
            "skipping systemd dispatch)."
        ),
    )
    parser.add_argument("--prefix", help="Root prefix for install paths")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install")

    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--purge-config", action="store_true")
    uninstall_parser.add_argument("--purge-state", action="store_true")
    uninstall_parser.add_argument("--purge-logs", action="store_true")

    sub.add_parser("check", aliases=["preflight"])
    sub.add_parser("doctor")
    sub.add_parser("run")
    sub.add_parser("start")
    sub.add_parser("status")

    list_parser = sub.add_parser("list-vms")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--include-blacklisted", action="store_true")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--vm")

    sub.add_parser("list-restore-points")

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument(
        "vm_uuid",
        help="VM libvirt UUID copied from the first column of list-restore-points output.",
    )
    restore_parser.add_argument(
        "timestamp",
        help="Per-run timestamp (YYYYMMDDTHHMMSS) copied from the second column of list-restore-points output.",
    )

    return parser


def _run_command(config: Config) -> int:
    # Acquire the run lock BEFORE preflight: the NBD probe drives QEMU's HMP
    # nbd_server_start/stop, so running it outside the lock could disrupt a
    # concurrent backup. ``check(..., lock_held=True)`` skips the duplicate
    # lock acquisition inside the probe.
    try:
        with acquire_run_lock(config):
            preflight_code = check(config, lock_held=True)
            if preflight_code != 0:
                return preflight_code
            backup_code = run_backups(config)
            # Pruning failure must not roll back successful backups, so we
            # combine codes via ``max`` rather than short-circuiting. Disabling
            # cleanup leaves retention entirely to operators / external tools.
            if not config.enabled("BACKUP_CLEANUP_ON_RUN"):
                return backup_code
            if backup_code != 0:
                # A failed run can leave the current month without a fresh
                # backup for the affected VMs. Pruning now would delete the
                # oldest still-good month while the newest month is incomplete,
                # so wait for a clean run before touching retention.
                event("info", "retention skipped because backups did not all succeed")
                return backup_code
            # Gate retention on the current calendar month landing a backup:
            # with retention=12 the oldest month only drops once month 13's
            # first full has actually written its chain dir, so a missed or
            # delayed run never collapses the window below the operator's
            # configured horizon.
            return max(backup_code, prune_old_months(config, current_month=current_month()))
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _restore_command(config: Config, args: argparse.Namespace) -> int:
    config_code = validate_config(config)
    if config_code != 0:
        return config_code
    try:
        with acquire_run_lock(config):
            return restore(config, args.vm_uuid, args.timestamp)
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
                    return verify(config, vm_name=args.vm)
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
