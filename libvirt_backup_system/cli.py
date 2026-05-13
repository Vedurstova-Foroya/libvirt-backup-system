from __future__ import annotations

import argparse
import contextlib
import json
import sys
import traceback
from pathlib import Path

from .backup import run_backups, verify
from .config import Config
from .installer import install, uninstall
from .lock import LockBusyError, acquire_run_lock
from .logging_json import event
from .preflight import check, validate_config
from .restore import restore
from .retention import prune_old_months
from .shell import configure_default_timeout
from .systemd_units import dispatch_via_systemd, status
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

    sub.add_parser("check", aliases=["preflight"])
    sub.add_parser("run")
    sub.add_parser("status")

    list_parser = sub.add_parser("list-vms")
    list_parser.add_argument("--json", action="store_true")
    list_parser.add_argument("--include-blacklisted", action="store_true")

    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--vm")

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--vm", required=True)
    restore_parser.add_argument("--output", required=True)
    restore_parser.add_argument("--month")
    restore_parser.add_argument("--chain")

    return parser


def _run_command(config: Config) -> int:
    preflight_code = check(config)
    if preflight_code != 0:
        return preflight_code
    try:
        with acquire_run_lock(config):
            backup_code = run_backups(config)
            # Pruning failure must not roll back successful backups, so we
            # combine codes via ``max`` rather than short-circuiting. Disabling
            # cleanup leaves retention entirely to operators / external tools.
            if not config.enabled("BACKUP_CLEANUP_ON_RUN"):
                return backup_code
            return max(backup_code, prune_old_months(config))
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _restore_command(config: Config, args: argparse.Namespace) -> int:
    config_code = validate_config(config)
    if config_code != 0:
        return config_code
    try:
        with acquire_run_lock(config):
            return restore(
                config,
                args.vm,
                Path(args.output),
                month=args.month,
                chain=args.chain,
            )
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            return install(args.prefix, config_path=args.config)
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
