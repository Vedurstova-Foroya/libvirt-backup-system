from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import cli, systemd_units
from libvirt_backup_system.systemd_units import (
    CHECK_UNIT_NAME,
    DISPATCH_OPT_OUT_ENV,
    RUN_UNIT_NAME,
    dispatch_via_systemd,
)


def _fake_systemd_host(tmp_path: Path, monkeypatch) -> Path:
    """Wire dispatch_via_systemd to a tmp-rooted "real" install layout.

    Returns the systemd unit directory under tmp_path so the caller can drop in
    or omit unit files to exercise the "unit present" / "unit absent" branches.
    """
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    monkeypatch.setattr("libvirt_backup_system.systemd_units.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.prefixed", lambda path, root: tmp_path / str(path).lstrip("/")
    )
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")
    original_exists = Path.exists
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.Path.exists",
        lambda self: True if str(self) == "/run/systemd/system" else original_exists(self),
    )
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv(DISPATCH_OPT_OUT_ENV, raising=False)
    return systemd_dir


def _record_subprocess(monkeypatch, *, start_returncode: int = 0, invocation_id: str = "deadbeef") -> list[list[str]]:
    """Stub subprocess.run inside systemd_units so dispatch_via_systemd is hermetic.

    Records every command observed; for ``systemctl start`` returns
    ``start_returncode``, for ``systemctl show -p InvocationID`` returns
    ``invocation_id`` so the follow-up journalctl call fires.
    """
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(args: list[str], **kwargs: Any) -> _Result:
        calls.append(args)
        if args[:3] == ["systemctl", "start", "--wait"]:
            return _Result(start_returncode)
        if args[:2] == ["systemctl", "show"]:
            return _Result(0, invocation_id + "\n")
        if args[:1] == ["journalctl"]:
            return _Result(0)
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_dispatch_skipped_when_inside_systemd_invocation(tmp_path: Path, monkeypatch) -> None:
    # Recursion guard: the unit's own ExecStart re-enters the CLI, and dispatch
    # must not re-dispatch — that would loop until systemd kills the unit.
    _fake_systemd_host(tmp_path, monkeypatch)
    (tmp_path / "etc/systemd/system" / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setenv("INVOCATION_ID", "abc")

    assert dispatch_via_systemd("check", prefix=None, config_path=None) is None


def test_dispatch_skipped_when_opt_out_env_set(tmp_path: Path, monkeypatch) -> None:
    _fake_systemd_host(tmp_path, monkeypatch)
    (tmp_path / "etc/systemd/system" / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setenv(DISPATCH_OPT_OUT_ENV, "1")

    assert dispatch_via_systemd("run", prefix=None, config_path=None) is None


def test_dispatch_skipped_when_prefix_passed(tmp_path: Path, monkeypatch) -> None:
    _fake_systemd_host(tmp_path, monkeypatch)
    (tmp_path / "etc/systemd/system" / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")

    assert dispatch_via_systemd("check", prefix="/somewhere/else", config_path=None) is None


def test_dispatch_skipped_when_config_overridden(tmp_path: Path, monkeypatch) -> None:
    # The installed unit has --config baked into ExecStart. A caller-supplied
    # --config means "use this other file", which the unit cannot honor.
    _fake_systemd_host(tmp_path, monkeypatch)
    (tmp_path / "etc/systemd/system" / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")

    assert dispatch_via_systemd("check", prefix=None, config_path="/etc/custom.env") is None


def test_dispatch_skipped_when_unit_not_installed(tmp_path: Path, monkeypatch) -> None:
    # systemd is available on the host but the unit was never installed (fresh
    # checkout, package not deployed). Must fall back to in-process so the
    # operator can still run ``check`` before installing.
    _fake_systemd_host(tmp_path, monkeypatch)

    assert dispatch_via_systemd("check", prefix=None, config_path=None) is None


def test_dispatch_skipped_when_systemctl_unavailable(tmp_path: Path, monkeypatch) -> None:
    _fake_systemd_host(tmp_path, monkeypatch)
    (tmp_path / "etc/systemd/system" / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: None)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) is None


def test_dispatch_invokes_systemctl_and_returns_exit_code(tmp_path: Path, monkeypatch, capsys) -> None:
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    calls = _record_subprocess(monkeypatch, start_returncode=2, invocation_id="abc123")

    rc = dispatch_via_systemd("check", prefix=None, config_path=None)

    assert rc == 2
    assert ["systemctl", "start", "--wait", CHECK_UNIT_NAME] in calls
    assert ["systemctl", "show", CHECK_UNIT_NAME, "--property=InvocationID", "--value"] in calls
    assert ["journalctl", "_SYSTEMD_INVOCATION_ID=abc123", "--output=cat", "--no-pager"] in calls
    # Dispatch announcement is emitted as an info-level structured event,
    # which logging_json.event routes to stdout (errors/warnings go to stderr).
    out = capsys.readouterr().out
    assert "dispatching to systemd unit" in out
    assert CHECK_UNIT_NAME in out


def test_dispatch_run_targets_run_unit(tmp_path: Path, monkeypatch) -> None:
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / RUN_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    calls = _record_subprocess(monkeypatch)

    assert dispatch_via_systemd("run", prefix=None, config_path=None) == 0
    assert calls[0] == ["systemctl", "start", "--wait", RUN_UNIT_NAME]


def test_dispatch_skips_journalctl_when_invocation_id_missing(tmp_path: Path, monkeypatch) -> None:
    # systemd-less or partially-built systemd setups can return an empty
    # InvocationID. Skip the journal tail rather than invoking journalctl with
    # an empty match string, which would dump unrelated entries.
    systemd_dir = _fake_systemd_host(tmp_path, monkeypatch)
    (systemd_dir / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    calls = _record_subprocess(monkeypatch, invocation_id="")

    assert dispatch_via_systemd("check", prefix=None, config_path=None) == 0
    assert not any(args[:1] == ["journalctl"] for args in calls)


def test_unit_name_for_rejects_unknown_subcommand() -> None:
    with pytest.raises(ValueError, match="no dispatch unit"):
        systemd_units.unit_name_for("status")


def test_render_unit_service_rejects_unknown_subcommand(tmp_path: Path) -> None:
    # render_unit_service is the only caller-facing entry point that consumes
    # the subcommand keyword; guarding it locally means a typo in installer.py
    # surfaces at install time with a clean error instead of producing a unit
    # file with ``ExecStart=... <typo>`` that systemd then refuses to load.
    with pytest.raises(ValueError, match="unknown unit subcommand"):
        systemd_units.render_unit_service(
            str(tmp_path),
            tmp_path / "usr/local/bin/libvirt-backup-system",
            tmp_path / "etc/config.env",
            subcommand="bogus",
        )


def test_cli_run_uses_dispatch_when_available(tmp_path: Path, monkeypatch) -> None:
    # End-to-end: ``main(["run"])`` consults dispatch_via_systemd before
    # falling through to the in-process Config.load + preflight path.
    called: dict[str, Any] = {}

    def fake_dispatch(subcommand: str, *, prefix: object, config_path: object) -> int:
        called["args"] = (subcommand, prefix, config_path)
        return 7

    monkeypatch.setattr("libvirt_backup_system.cli.dispatch_via_systemd", fake_dispatch)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load",
        lambda *args, **kwargs: pytest.fail("Config.load must not run when dispatch succeeds"),
    )

    assert cli.main(["run"]) == 7
    assert called["args"] == ("run", None, None)


def test_cli_check_falls_through_to_in_process_when_dispatch_returns_none(tmp_path: Path, monkeypatch) -> None:
    from libvirt_backup_system.config import DEFAULTS, Config

    monkeypatch.setattr("libvirt_backup_system.cli.dispatch_via_systemd", lambda *a, **k: None)
    in_process_called: dict[str, bool] = {}

    def fake_check(config: object) -> int:
        in_process_called["yes"] = True
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.check", fake_check)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load",
        lambda *args, **kwargs: Config(values=dict(DEFAULTS), path=tmp_path / "x.env", prefix=tmp_path),
    )

    assert cli.main(["check"]) == 0
    assert in_process_called == {"yes": True}
