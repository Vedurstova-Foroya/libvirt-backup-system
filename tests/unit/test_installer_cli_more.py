from __future__ import annotations

import contextlib
from pathlib import Path

from libvirt_backup_system.cli import main
from libvirt_backup_system.config import DEFAULTS, Config


def _fake_config(tmp_path: Path) -> Config:
    return Config(values=dict(DEFAULTS), path=tmp_path / "config.env", prefix=tmp_path)


def test_cli_doctor_returns_doctor_exit_code(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.doctor", lambda config: 9)
    assert main(["doctor"]) == 9


def test_cli_list_restore_points_validate_failure(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 5)
    assert main(["list-restore-points"]) == 5


def test_cli_list_restore_points_runs_command(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr("libvirt_backup_system.cli.list_restore_points", lambda config: 0)
    assert main(["list-restore-points"]) == 0


def test_cli_change_password_delegates_to_installer_password(tmp_path: Path, monkeypatch) -> None:
    # ``main(["change-password", ...])`` must build a ``PasswordSpec`` from the
    # ``--new-kopia-password*`` flags and hand it to ``installer_password``
    # under the run lock. We verify the spec round-trips end-to-end so a flag
    # rename can't silently drop the new password.
    from libvirt_backup_system.kopia_password import PasswordSpec

    captured: dict[str, object] = {}

    def fake_change(config: object, spec: PasswordSpec) -> int:
        captured["config"] = config
        captured["spec"] = spec
        return 0

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load",
        lambda config_path=None, prefix=None: _fake_config(tmp_path),
    )
    monkeypatch.setattr("libvirt_backup_system.cli._change_password_impl", fake_change)
    assert main(["--prefix", str(tmp_path), "change-password", "--new-kopia-password", "rotated"]) == 0
    spec = captured["spec"]
    assert isinstance(spec, PasswordSpec)
    assert spec.literal == "rotated"
    assert spec.file is None
    assert spec.env_var is None


def test_cli_change_password_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    # change-password holds the run lock so concurrent backups never observe a
    # half-rotated repo. The lock-busy branch must surface the same message
    # the rest of the CLI uses rather than letting the rotation race.
    from libvirt_backup_system.lock import LockBusyError

    monkeypatch.setattr(
        "libvirt_backup_system.cli.Config.load",
        lambda config_path=None, prefix=None: _fake_config(tmp_path),
    )

    @contextlib.contextmanager
    def busy(config: object):
        raise LockBusyError(tmp_path / "run.lock")
        yield  # pragma: no cover

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", busy)
    monkeypatch.setattr(
        "libvirt_backup_system.cli._change_password_impl",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not run while locked")),
    )
    assert main(["--prefix", str(tmp_path), "change-password", "--new-kopia-password", "x"]) == 1
    err = capsys.readouterr().err
    assert "another run in progress" in err
    assert "run.lock" in err
