from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import traceback
from pathlib import Path

from . import kopia_repo
from .backup import run_backups
from .cli_parser import build_parser
from .cli_parser import password_spec_from_args as _password_spec_from_args
from .config import Config
from .doctor import doctor
from .installer import install, uninstall
from .installer_password import change_password as _change_password_impl
from .kopia_client import KOPIA_BINARY, build_kopia_env
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

__all__ = ["build_parser", "main"]


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


def _change_password_command(args: argparse.Namespace) -> int:
    config = Config.load(config_path=args.config, prefix=args.prefix)
    spec = _password_spec_from_args(args, prefix="new_")
    try:
        with acquire_run_lock(config):
            return _change_password_impl(config, spec)
    except LockBusyError as exc:
        event("error", "another run in progress", lock_path=str(exc.path))
        return 1


def _resolve_passthrough_config_file(config: Config, host_id: str | None) -> Path | None:
    """Resolve the kopia connection-config to use for kopia-passthrough.

    Defaults to the local repo's config file. When ``host_id`` is supplied
    the peer repo is connected (read-only) under the matching peer config
    file, mirroring how ``list-restore-points`` reaches peer repos.
    """
    if host_id is None:
        return kopia_repo.local_config_file(config)
    peer_config = kopia_repo.ensure_peer_connected(config, host_id)
    if peer_config is None:
        event("error", "kopia-passthrough peer repo not reachable", host_id=host_id)
        return None
    return peer_config


def _kopia_passthrough_command(args: argparse.Namespace, config: Config) -> int:
    """Exec the kopia binary with the operator's argv tail against a managed repo.

    Inherits the parent's stdin/stdout/stderr so an interactive ``kopia``
    invocation behaves the same as if the operator had run it by hand.
    Exit code propagates from ``kopia`` so shells and CI can branch on it.
    """
    tail = list(args.kopia_args or [])
    # argparse.REMAINDER preserves a leading ``--`` if the operator wrote one
    # so kopia's own flags don't get captured by us. Strip a single leading
    # ``--`` so the actual kopia argv starts at the real command.
    if tail and tail[0] == "--":
        tail = tail[1:]
    if not tail:
        event("error", "kopia-passthrough requires at least one kopia argument")
        return 2
    config_file = _resolve_passthrough_config_file(config, args.host_id)
    if config_file is None:
        return 1
    password_file = kopia_repo.password_file_path(config)
    cache_dir = kopia_repo.cache_dir(config)
    try:
        env = build_kopia_env(password_file, cache_dir)
    except Exception as exc:
        event("error", "kopia-passthrough password unreadable", error=str(exc))
        return 1
    cmd = [KOPIA_BINARY, f"--config-file={config_file}", *tail]
    completed = subprocess.run(cmd, check=False, env=env)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "install":
            return install(
                args.prefix,
                config_path=args.config,
                password_spec=_password_spec_from_args(args, prefix=""),
            )
        if args.command == "change-password":
            return _change_password_command(args)
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
            # Hold the same run-lock as ``run``: a concurrent run can expose
            # an in-flight kopia snapshot and produce a confusing verify
            # error. The lock surfaces "another run in progress" instead.
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
        if args.command == "kopia-passthrough":
            return _kopia_passthrough_command(args, config)
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
