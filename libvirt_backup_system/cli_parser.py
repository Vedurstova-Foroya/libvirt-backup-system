from __future__ import annotations

import argparse

from . import cli_help
from .kopia_password import PasswordSpec


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


def _add_password_flags(parser: argparse.ArgumentParser, *, prefix: str) -> None:
    """Add ``--{prefix}kopia-password*`` flags to ``parser`` as a mutex group."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        f"--{prefix}kopia-password",
        metavar="VALUE",
        help="Shared kopia repo password (visible in ps/journald; prefer the file/env forms).",
    )
    group.add_argument(
        f"--{prefix}kopia-password-file",
        metavar="PATH",
        help="Path to a file holding the password; '-' reads stdin.",
    )
    group.add_argument(
        f"--{prefix}kopia-password-env",
        metavar="VAR",
        help="Environment variable name holding the password.",
    )


def password_spec_from_args(args: argparse.Namespace, *, prefix: str) -> PasswordSpec:
    return PasswordSpec(
        literal=getattr(args, f"{prefix}kopia_password", None),
        file=getattr(args, f"{prefix}kopia_password_file", None),
        env_var=getattr(args, f"{prefix}kopia_password_env", None),
        acknowledge_loss=getattr(args, "acknowledge_password_loss", False),
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

    install_parser = _add_subparser(
        sub, "install", help_text=cli_help.INSTALL_HELP, description=cli_help.INSTALL_DESCRIPTION
    )
    _add_password_flags(install_parser, prefix="")
    install_parser.add_argument(
        "--acknowledge-password-loss",
        action="store_true",
        help=(
            "Required on first install: confirms this password has been stored outside "
            "libvirt-backup-system and that losing it makes all backups unrecoverable."
        ),
    )

    change_password_parser = _add_subparser(
        sub,
        "change-password",
        help_text=cli_help.CHANGE_PASSWORD_HELP,
        description=cli_help.CHANGE_PASSWORD_DESCRIPTION,
    )
    _add_password_flags(change_password_parser, prefix="new-")

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

    restore_points_parser = _add_subparser(
        sub,
        "list-restore-points",
        help_text=cli_help.LIST_RESTORE_POINTS_HELP,
        description=cli_help.LIST_RESTORE_POINTS_DESCRIPTION,
    )
    restore_points_parser.add_argument("--json", action="store_true", help="Emit JSON array instead of table rows.")

    restore_parser = _add_subparser(
        sub, "restore", help_text=cli_help.RESTORE_HELP, description=cli_help.RESTORE_DESCRIPTION
    )
    restore_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Stream full restore output instead of only summary success/error events.",
    )
    restore_parser.add_argument(
        "--host-id",
        metavar="HOST_ID",
        help="Disambiguate duplicate VM UUID/timestamp matches by source-host-id.",
    )
    restore_parser.add_argument(
        "--run-id",
        metavar="RUN_ID",
        help="Disambiguate duplicate VM UUID/timestamp matches by run-id.",
    )
    restore_parser.add_argument(
        "vm_uuid",
        metavar="VM_UUID",
        help="VM libvirt UUID copied verbatim from the vm-uuid column of list-restore-points output.",
    )
    restore_parser.add_argument(
        "timestamp",
        metavar="TIMESTAMP",
        help=(
            "Per-run timestamp (YYYYMMDDTHHMMSS) copied verbatim from the timestamp column of "
            "list-restore-points output. Exact match against the meta snapshot's timestamp tag."
        ),
    )

    # Hidden ad-hoc escape hatch: ``kopia-passthrough`` shells out to the
    # ``kopia`` binary against a managed repo. Marked ``help=SUPPRESS`` so it
    # does not pollute the top-level subcommand listing; documented in
    # ``cli_help.KOPIA_PASSTHROUGH_DESCRIPTION`` for operators who go looking.
    kopia_parser = sub.add_parser(
        "kopia-passthrough",
        help=argparse.SUPPRESS,
        description=cli_help.KOPIA_PASSTHROUGH_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    kopia_parser.add_argument(
        "--host-id",
        metavar="HOST_ID",
        default=None,
        help=argparse.SUPPRESS,
    )
    kopia_parser.add_argument(
        "kopia_args",
        nargs=argparse.REMAINDER,
        metavar="-- KOPIA_ARGS...",
        help="Arguments forwarded verbatim to the kopia binary.",
    )
    sub._choices_actions = [action for action in sub._choices_actions if action.dest != "kopia-passthrough"]  # noqa: SLF001

    return parser
