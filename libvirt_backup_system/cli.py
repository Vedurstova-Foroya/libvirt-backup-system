from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback

from .backup import cleanup, run_backups, verify
from .config import Config
from .installer import install, uninstall
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .preflight import check, validate_config
from .vms import list_vms


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="libvirt-backup-system")
    parser.add_argument("--config", help="Path to libvirt-backup.env")
    parser.add_argument("--prefix", help="Root prefix for install paths")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install")

    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--purge-config", action="store_true")
    uninstall_parser.add_argument("--purge-state", action="store_true")
    uninstall_parser.add_argument("--purge-logs", action="store_true")
    uninstall_parser.add_argument("--purge-backups", action="store_true")

    sub.add_parser("check", aliases=["preflight"])
    sub.add_parser("run")

    list_parser = sub.add_parser("list-vms")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--include-blacklisted", action="store_true")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--vm")

    sub.add_parser("cleanup")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            return install(args.prefix, config_path=args.config)
        if args.command == "uninstall":
            return uninstall(
                args.prefix,
                config_path=args.config,
                purge_config=args.purge_config,
                purge_state=args.purge_state,
                purge_logs=args.purge_logs,
                purge_backups=args.purge_backups,
            )

        if args.command == "list-vms" and args.json:
            with contextlib.redirect_stdout(sys.stderr):
                config = Config.load(config_path=args.config, prefix=args.prefix)
        else:
            config = Config.load(config_path=args.config, prefix=args.prefix)
        if args.command in {"check", "preflight"}:
            return check(config)
        if args.command == "run":
            preflight_code = check(config)
            if preflight_code != 0:
                return preflight_code
            try:
                with acquire_run_lock(config):
                    backup_code = run_backups(config)
                    if backup_code != 0:
                        event("warning", "cleanup skipped because backups failed")
                        return backup_code
                    return cleanup(config)
            except LockBusyError as exc:
                event("error", "another run in progress", lock_path=str(exc.path))
                return 1
        if args.command == "list-vms":
            config_code = validate_config(config)
            if config_code != 0:
                return config_code
            vms = list_vms(config, include_blacklisted=args.include_blacklisted)
            if args.json:
                print(json.dumps([{"name": vm.name, "state": vm.state, "running": vm.running} for vm in vms]))
            else:
                for vm in vms:
                    print(f"{vm.name}\t{vm.state}")
            return 0
        if args.command == "verify":
            config_code = validate_config(config)
            if config_code != 0:
                return config_code
            return verify(config, vm_name=args.vm)
        if args.command == "cleanup":
            config_code = validate_config(config)
            if config_code != 0:
                return config_code
            return cleanup(config)
    except KeyboardInterrupt:
        event("error", "interrupted")
        return 130
    except Exception as exc:
        event("error", "fatal error", error=str(exc), traceback=traceback.format_exc())
        return 1

    parser.print_help(sys.stderr)
    return 2
