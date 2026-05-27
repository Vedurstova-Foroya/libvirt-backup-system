"""Core typed wrapper around the ``kopia`` CLI.

Repository, policy, and maintenance subcommands live here; snapshot
operations (create / list / restore / verify) live in
``kopia_snapshots.py``. The split keeps each file well under the project's
300-LOC ceiling without breaking the natural grouping.

Every public function takes an explicit ``config_file: Path`` identifying
which repo connection-config to use. Subcommands shell out via
``shell.run`` / ``shell.run_streamed`` so unit tests inject command
results through ``monkeypatch.setattr`` on this module's names. JSON
shapes are pinned in ``tests/unit/fixtures/`` so a kopia version bump
that changes a shape is caught by a fixture diff.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from . import kopia_password
from .logging_json import event
from .shell import CommandError, CommandResult, run, run_streamed

KOPIA_BINARY = "kopia"


def as_string_keyed(value: object) -> dict[str, object]:
    """Coerce a parsed-JSON object into ``dict[str, object]`` shape.

    JSON object keys are always strings; ``json.loads`` returns
    ``dict[Unknown, Unknown]`` under strict pyright. A cast after the
    isinstance check stays sound because the runtime guarantees string keys.
    """
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", value)


def as_string_string(value: object) -> dict[str, str]:
    raw = as_string_keyed(value)
    return {key: val for key, val in raw.items() if isinstance(val, str)}


def build_kopia_env(password_file: Path, cache_dir: Path | None) -> dict[str, str]:
    # kopia reads the password from KOPIA_PASSWORD or a --password-file path.
    # Going through KOPIA_PASSWORD avoids exposing the file path as an argv
    # token; the env-only secret survives ``ps`` and stays out of journald.
    env = dict(os.environ)
    try:
        env["KOPIA_PASSWORD"] = kopia_password.read_secure_password_file(password_file)
    except (OSError, ValueError) as exc:
        raise CommandError(
            CommandResult(args=[KOPIA_BINARY], returncode=1, stdout="", stderr=f"kopia password unreadable: {exc}")
        ) from exc
    if cache_dir is not None:
        env["KOPIA_CACHE_DIRECTORY"] = str(cache_dir)
    # The binary will otherwise phone home on every invocation. We pin
    # versions deliberately.
    env["KOPIA_CHECK_FOR_UPDATES"] = "false"
    return env


def build_config_args(config_file: Path) -> list[str]:
    return ["--config-file", str(config_file)]


def run_kopia(
    args: list[str],
    *,
    password_file: Path,
    cache_dir: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> CommandResult:
    env = build_kopia_env(password_file, cache_dir)
    return run([KOPIA_BINARY, *args], check=check, env=env, timeout=timeout)


def run_kopia_streamed(
    args: list[str],
    *,
    password_file: Path,
    cache_dir: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> CommandResult:
    env = build_kopia_env(password_file, cache_dir)
    return run_streamed([KOPIA_BINARY, *args], check=check, env=env, timeout=timeout)


def repository_create_filesystem(
    *,
    config_file: Path,
    repo_path: Path,
    password_file: Path,
    cache_dir: Path | None = None,
) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    run_kopia(
        [*build_config_args(config_file), "repository", "create", "filesystem", "--path", str(repo_path)],
        password_file=password_file,
        cache_dir=cache_dir,
    )


def repository_connect_filesystem(
    *,
    config_file: Path,
    repo_path: Path,
    password_file: Path,
    cache_dir: Path | None = None,
    read_only: bool = False,
) -> None:
    args = [*build_config_args(config_file), "repository", "connect", "filesystem", "--path", str(repo_path)]
    if read_only:
        args.append("--readonly")
    run_kopia(args, password_file=password_file, cache_dir=cache_dir)


def repository_status(*, config_file: Path, password_file: Path, cache_dir: Path | None = None) -> dict[str, object]:
    result = run_kopia(
        [*build_config_args(config_file), "repository", "status", "--json"],
        password_file=password_file,
        cache_dir=cache_dir,
    )
    parsed: object = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("kopia repository status returned a non-object JSON document")
    return as_string_keyed(cast("object", parsed))


def repository_change_password(
    *,
    config_file: Path,
    password_file: Path,
    new_password: str,
    cache_dir: Path | None = None,
) -> None:
    """Wrap the master key under ``new_password``.

    ``kopia repository change-password`` ordinarily prompts on stdin. We
    drive it with ``Popen`` so we can pipe the new password in instead of
    putting it on argv.
    """
    env = build_kopia_env(password_file, cache_dir)
    args = [KOPIA_BINARY, *build_config_args(config_file), "repository", "change-password"]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    stdout, stderr = proc.communicate(f"{new_password}\n{new_password}\n")
    if proc.returncode != 0:
        raise CommandError(
            CommandResult(args=args, returncode=proc.returncode, stdout=stdout or "", stderr=stderr or "")
        )


def policy_set_global(
    *,
    config_file: Path,
    password_file: Path,
    cache_dir: Path | None = None,
    keep_latest: int | None = None,
    keep_hourly: int | None = None,
    keep_daily: int | None = None,
    keep_weekly: int | None = None,
    keep_monthly: int | None = None,
    keep_annual: int | None = None,
    compression: str | None = None,
    splitter: str | None = None,
) -> None:
    args = [*build_config_args(config_file), "policy", "set", "--global"]
    baseline = len(args)
    for flag, value in (
        ("--keep-latest", keep_latest),
        ("--keep-hourly", keep_hourly),
        ("--keep-daily", keep_daily),
        ("--keep-weekly", keep_weekly),
        ("--keep-monthly", keep_monthly),
        ("--keep-annual", keep_annual),
    ):
        if value is not None:
            args.extend([flag, str(value)])
    if compression is not None:
        args.extend(["--compression", compression])
    if splitter is not None:
        args.extend(["--splitter", splitter])
    if len(args) <= baseline:
        return
    run_kopia(args, password_file=password_file, cache_dir=cache_dir)


def policy_show_global(*, config_file: Path, password_file: Path, cache_dir: Path | None = None) -> dict[str, object]:
    result = run_kopia(
        [*build_config_args(config_file), "policy", "show", "--global", "--json"],
        password_file=password_file,
        cache_dir=cache_dir,
    )
    parsed: object = json.loads(result.stdout)
    if not isinstance(parsed, dict):
        raise ValueError("kopia policy show returned a non-object JSON document")
    return as_string_keyed(cast("object", parsed))


def maintenance_run(
    *,
    config_file: Path,
    password_file: Path,
    cache_dir: Path | None = None,
    full: bool = False,
    safety: str | None = None,
    dry_run: bool = False,
) -> None:
    args = [*build_config_args(config_file), "maintenance", "run"]
    if full:
        args.append("--full")
    if safety is not None:
        args.append(f"--safety={safety}")
    if dry_run:
        # ``maintenance run --dry-run`` reports what would be deleted/rewritten
        # without touching the repo. doctor invokes this to surface scheduled
        # maintenance issues without races against the live unit.
        args.append("--dry-run")
    run_kopia_streamed(args, password_file=password_file, cache_dir=cache_dir)


def maintenance_set_owner(*, config_file: Path, password_file: Path, owner: str, cache_dir: Path | None = None) -> None:
    """Claim ``owner`` (typically ``HOST_ID``) as the maintenance owner."""
    args = [*build_config_args(config_file), "maintenance", "set", f"--owner={owner}"]
    run_kopia(args, password_file=password_file, cache_dir=cache_dir)


def kopia_available() -> bool:
    """Return True if the ``kopia`` binary is on PATH and runs ``--version``."""
    try:
        result = run([KOPIA_BINARY, "--version"], check=False, timeout=10)
    except OSError as exc:
        event("info", "kopia binary unavailable", error=str(exc))
        return False
    return result.returncode == 0


def tags_args(tags: Mapping[str, str]) -> list[str]:
    """Render ``--tags=key:value`` for kopia subcommands.

    Public-named because ``kopia_snapshots`` consumes it; pyright otherwise
    flags a leading-underscore name imported across modules as unused.
    """
    return [f"--tags={key}:{value}" for key, value in sorted(tags.items())]
