from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from libvirt_backup_system.cli import build_parser, main
from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.list_restore_points import BackupEnumeration, BackupRow


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
    seen: dict[str, bool] = {}

    def fake_list_restore_points(config, *, json_output=False):
        seen["json_output"] = json_output
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.list_restore_points", fake_list_restore_points)
    assert main(["list-restore-points"]) == 0
    assert seen["json_output"] is False


def test_cli_list_restore_points_json_keeps_logs_off_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _fake_config(tmp_path)
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: print("config log") or 0)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.enumerate_backups_result",
        lambda config: BackupEnumeration([], ok=True),
    )
    assert main(["list-restore-points", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "[]\n"
    assert "config log" in captured.err


def test_cli_list_restore_points_json_allows_peer_rows_when_local_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = _fake_config(tmp_path)
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    cfg.values["BACKUP_PATH"] = str(backup_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    cfg.values["HOST_ID"] = "host-a"
    row = BackupRow(
        vm_uuid="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        timestamp="20260101T010101",
        host_id="host-b",
        vm_name="peer-vm",
        run_id="run-1",
        snapshot_id="snap",
        config_file=tmp_path / "peer.config",
    )
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)
    monkeypatch.setattr(
        "libvirt_backup_system.cli.enumerate_backups_result",
        lambda config: BackupEnumeration([row], ok=False, failed_host_ids=("host-a",)),
    )

    assert main(["list-restore-points", "--json"]) == 0
    assert '"source_host_id": "host-b"' in capsys.readouterr().out


def test_cli_restore_forwards_disambiguators(tmp_path: Path, monkeypatch) -> None:
    cfg = _fake_config(tmp_path)
    captured: dict[str, object] = {}
    monkeypatch.setattr("libvirt_backup_system.cli.Config.load", lambda config_path=None, prefix=None: cfg)
    monkeypatch.setattr("libvirt_backup_system.cli.validate_config", lambda config: 0)

    @contextlib.contextmanager
    def fake_lock(config: object):
        assert config is cfg
        yield Path("/tmp/fake.lock")

    def fake_restore(config: object, vm_uuid: str, timestamp: str, **kwargs: object) -> int:
        captured.update({"config": config, "vm_uuid": vm_uuid, "timestamp": timestamp, **kwargs})
        return 0

    monkeypatch.setattr("libvirt_backup_system.cli.acquire_run_lock", fake_lock)
    monkeypatch.setattr("libvirt_backup_system.cli.restore", fake_restore)
    assert main(["restore", "--host-id", "host-b", "--run-id", "run-2", row_uuid(), "20260507T101112"]) == 0
    assert captured == {
        "config": cfg,
        "vm_uuid": row_uuid(),
        "timestamp": "20260507T101112",
        "host_id": "host-b",
        "run_id": "run-2",
        "verbose": False,
    }


def row_uuid() -> str:
    return "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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
    assert (
        main(
            [
                "--prefix",
                str(tmp_path),
                "change-password",
                "--new-kopia-password",
                "rotated",
            ]
        )
        == 0
    )
    spec = captured["spec"]
    assert isinstance(spec, PasswordSpec)
    assert spec.literal == "rotated"
    assert spec.file is None
    assert spec.env_var is None
    assert spec.acknowledge_argv_exposure is False


def test_cli_change_password_help_documents_kopia_argv_limitation(capsys) -> None:
    parser = build_parser()
    with contextlib.suppress(SystemExit):
        parser.parse_args(["change-password", "--help"])
    out = capsys.readouterr().out
    assert "Kopia's documented noninteractive rotation interface" in out
    assert "Kopia's argv" in out


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


def test_cli_change_password_missing_current_password_file_reports_operator_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("KOPIA_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(
        "libvirt_backup_system.kopia_repo.ensure_local_connected",
        lambda _cfg: pytest.fail("must not connect without current password"),
    )
    assert (
        main(
            [
                "--prefix",
                str(tmp_path),
                "change-password",
                "--new-kopia-password",
                "rotated",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "current kopia password file unreadable" in err
    assert "kopia password file missing" in err
    assert "fatal error" not in err
    assert "traceback" not in err
    assert "rotated" not in err


def test_cli_change_password_insecure_current_password_file_reports_operator_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.delenv("KOPIA_PASSWORD_FILE", raising=False)
    pw_path = tmp_path / "etc/libvirt-backup-system/kopia.pw"
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text("old-secret\n", encoding="utf-8")
    pw_path.chmod(0o644)
    monkeypatch.setattr(
        "libvirt_backup_system.kopia_repo.ensure_local_connected",
        lambda _cfg: pytest.fail("must not connect with insecure current password"),
    )
    assert (
        main(
            [
                "--prefix",
                str(tmp_path),
                "change-password",
                "--new-kopia-password",
                "rotated",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "current kopia password file unreadable" in err
    assert "must be mode 600" in err
    assert "fatal error" not in err
    assert "traceback" not in err
    assert "old-secret" not in err
    assert "rotated" not in err
