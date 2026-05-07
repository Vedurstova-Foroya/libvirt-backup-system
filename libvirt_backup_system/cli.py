from __future__ import annotations

import argparse
import json
import sys

from .backup import cleanup, restore_to_dir, run_backups, verify
from .config import Config
from .installer import install, uninstall
from .logging_json import event
from .preflight import check
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

    sub.add_parser("preflight")
    sub.add_parser("run")

    list_parser = sub.add_parser("list-vms")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--include-blacklisted", action="store_true")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--vm")

    sub.add_parser("cleanup")

    restore_parser = sub.add_parser("restore-to-dir")
    restore_parser.add_argument("source")
    restore_parser.add_argument("target")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            return install(args.prefix)
        if args.command == "uninstall":
            return uninstall(
                args.prefix,
                purge_config=args.purge_config,
                purge_state=args.purge_state,
                purge_logs=args.purge_logs,
                purge_backups=args.purge_backups,
            )

        config = Config.load(config_path=args.config, prefix=args.prefix)
        if args.command == "preflight":
            return check(config)
        if args.command == "run":
            preflight_code = check(config)
            if preflight_code != 0:
                return preflight_code
            return run_backups(config)
        if args.command == "list-vms":
            vms = list_vms(config, include_blacklisted=args.include_blacklisted)
            if args.json:
                print(json.dumps([{"name": vm.name, "state": vm.state, "running": vm.running} for vm in vms]))
            else:
                for vm in vms:
                    print(f"{vm.name}\t{vm.state}")
            return 0
        if args.command == "verify":
            return verify(config, vm_name=args.vm)
        if args.command == "cleanup":
            return cleanup(config)
        if args.command == "restore-to-dir":
            return restore_to_dir(args.source, args.target)
    except KeyboardInterrupt:
        event("error", "interrupted")
        return 130
    except Exception as exc:
        event("error", "fatal error", error=str(exc))
        return 1

    parser.print_help(sys.stderr)
    return 2
