from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import verify
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def _verify_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
        }
    )
    return cfg


def test_verify_success_failure_and_vm_filter(tmp_path: Path, monkeypatch, backup_config) -> None:
    cfg = _verify_config(backup_config)
    good = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good"
    bad = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/bad"
    good.mkdir(parents=True)
    bad.mkdir()

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        # virtnbdrestore is called with ``-a verify -i <dir> -o <dir>`` so the
        # backup-dir argument lives at the position after ``-i``.
        input_path = args[args.index("-i") + 1]
        if input_path in {str(bad), str(tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/was-bad")}:
            raise CommandError(CommandResult(args, 2, "", "bad"))
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.verify.run_streamed", fake_run)
    # vm_name=<uuid> exercises the literal-subdir path (no virsh round-trip).
    assert verify(cfg, vm_name=ALPHA_UUID) == 1
    bad.rename(tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/was-bad")
    assert verify(cfg) == 1
    assert verify(cfg, vm_name="missing") == 1


def test_verify_missing_root_reports_no_backups(backup_config) -> None:
    cfg = _verify_config(backup_config)

    assert verify(cfg) == 1


def test_verify_skips_non_directory_entries_without_vm_filter(tmp_path: Path, backup_config) -> None:
    cfg = _verify_config(backup_config)
    root = tmp_path / "backups/host"
    root.mkdir(parents=True)
    (root / "not-a-vm").write_text("file\n", encoding="utf-8")

    assert verify(cfg) == 1


def test_verify_rejects_invalid_vm_name(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _verify_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.verify.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert verify(cfg, vm_name="../escape") == 1
    err = capsys.readouterr().err
    assert "verify target name is invalid" in err

    assert verify(cfg, vm_name="-evil") == 1
    assert verify(cfg, vm_name="alpha/sub") == 1
    assert verify(cfg, vm_name=".") == 1


def test_verify_skips_unsafe_vm_root(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _verify_config(backup_config)
    (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good").mkdir(parents=True)
    monkeypatch.setattr(
        "libvirt_backup_system.verify.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    monkeypatch.setattr("libvirt_backup_system.verify.subpath_is_safe", lambda root, path: False)

    assert verify(cfg, vm_name=ALPHA_UUID) == 1
    assert "verify skipped because path is unsafe" in capsys.readouterr().err


def test_verify_resolves_name_to_uuid_via_virsh(tmp_path: Path, monkeypatch, backup_config) -> None:
    # ``verify --vm <name>`` for a still-extant VM must round-trip through
    # virsh to find its UUID dir; only the UUID layout exists on disk.
    cfg = _verify_config(backup_config)
    (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good").mkdir(parents=True)
    monkeypatch.setattr("libvirt_backup_system.verify.resolve_vm_uuid", lambda config, name: ALPHA_UUID)
    monkeypatch.setattr(
        "libvirt_backup_system.verify.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    assert verify(cfg, vm_name="alpha") == 0


def test_verify_reports_when_resolved_uuid_dir_is_missing(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    # virsh resolves the name but no backups have been written for that UUID
    # yet (or the dir was deleted out-of-band). Surface a clean "not found".
    cfg = _verify_config(backup_config)
    (tmp_path / "backups/host").mkdir(parents=True)  # exists but no UUID subdir
    monkeypatch.setattr("libvirt_backup_system.verify.resolve_vm_uuid", lambda config, name: ALPHA_UUID)

    assert verify(cfg, vm_name="alpha") == 1
    assert "verify target not found" in capsys.readouterr().err


def test_verify_skips_unsafe_month_dir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _verify_config(backup_config)
    (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good").mkdir(parents=True)
    monkeypatch.setattr(
        "libvirt_backup_system.verify.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    checks = iter([True, False])
    monkeypatch.setattr("libvirt_backup_system.verify.subpath_is_safe", lambda root, path: next(checks, True))

    assert verify(cfg) == 1
    assert "verify skipped because month path is unsafe" in capsys.readouterr().err


def test_verify_skips_unsafe_backup_dir(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _verify_config(backup_config)
    (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good").mkdir(parents=True)
    monkeypatch.setattr(
        "libvirt_backup_system.verify.run_streamed",
        lambda args, check=True, env=None: CommandResult(args, 0, "", ""),
    )
    checks = iter([True, True, False])
    monkeypatch.setattr("libvirt_backup_system.verify.subpath_is_safe", lambda root, path: next(checks, True))

    assert verify(cfg) == 1
    assert "verify skipped because backup path is unsafe" in capsys.readouterr().err
