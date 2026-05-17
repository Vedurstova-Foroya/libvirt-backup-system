from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID, BETA_UUID, virtnbdbackup_fake_success


def _backup_config(cfg: Config) -> Config:
    cfg.values.update(
        {
            "BACKUP_COMPRESS": "true",
            "INACTIVE_COPY_EVERY_RUN": "false",
        }
    )
    return cfg


def test_backup_vm_replaces_symlinked_inactive_marker_without_touching_target(
    tmp_path: Path,
    monkeypatch,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / f"backups/host/{BETA_UUID}/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    target = tmp_path / "outside-target"
    target.write_text("keep\n", encoding="utf-8")
    marker.symlink_to(target)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.inactive_marker_is_fresh",
        lambda uri, name, m: (_ for _ in ()).throw(AssertionError("symlink marker must not be checked")),
    )

    assert backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    assert calls
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert not marker.is_symlink()
    assert marker.read_text(encoding="utf-8") == "stamp\nfp-stub\n"


def test_backup_vm_recopies_when_inactive_marker_lstat_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / f"backups/host/{BETA_UUID}/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("old\n", encoding="utf-8")
    original_lstat = Path.lstat
    calls: list[list[str]] = []

    def fake_lstat(self: Path) -> os.stat_result:
        if self == marker:
            raise OSError("lstat denied")
        return original_lstat(self)

    monkeypatch.setattr("libvirt_backup_system.backup.Path.lstat", fake_lstat)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )

    assert backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    assert calls
    assert "inactive marker check failed" in capsys.readouterr().err


def test_backup_vm_recopies_when_inactive_marker_backup_dir_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / f"backups/host/{BETA_UUID}/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("oldstamp\nfp-stub\n", encoding="utf-8")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            calls.append(args),
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[2],
    )
    monkeypatch.setattr("libvirt_backup_system.backup.inactive_marker_is_fresh", lambda uri, name, m: True)

    assert backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "newstamp")

    assert calls
    assert marker.read_text(encoding="utf-8") == "newstamp\nfp-stub\n"
    out = capsys.readouterr().out
    assert "inactive marker backup directory is missing" in out
    assert "inactive VM already copied" not in out


def test_backup_vm_logs_inactive_marker_removal_failure(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    marker = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/.inactive-copy-complete"
    marker.parent.mkdir(parents=True)
    marker.write_text("old\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == marker:
            raise OSError("unlink denied")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.backup.Path.unlink", fake_unlink)
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)

    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "inactive marker removal failed" in capsys.readouterr().err


def test_backup_vm_fails_when_marker_path_becomes_unsafe(monkeypatch, capsys, backup_config) -> None:
    cfg = _backup_config(backup_config)
    # Four subpath-safety checks during an inactive copy; flip the last
    # (finalize-time) so the marker write is the aborted step.
    checks = iter([True, True, True, False])
    monkeypatch.setattr("libvirt_backup_system.backup.backup_subpath_is_safe", lambda config, path: next(checks))
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    assert "inactive marker skipped because destination became unsafe" in capsys.readouterr().err


def test_backup_vm_fails_when_domain_xml_changes_during_backup(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    fingerprints = iter(["pre-fp", "post-fp"])
    monkeypatch.setattr(
        "libvirt_backup_system.backup.domain_xml_fingerprint",
        lambda uri, name: next(fingerprints),
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )

    written: list[tuple[str, str]] = []

    def fake_write(marker, stamp, fingerprint, vm):
        written.append((stamp, fingerprint))
        return True

    monkeypatch.setattr("libvirt_backup_system.backup.write_marker", fake_write)

    # XML drift mid-copy must fail the VM so the run is non-zero; otherwise
    # a backup whose configuration no longer matches the live domain would be
    # recorded as a real success.
    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    captured = capsys.readouterr()
    assert "domain XML changed during inactive backup; backup not trusted" in captured.err
    assert written == []
    assert not (tmp_path / f"backups/host/{BETA_UUID}/2026-05/stamp").exists()


def test_inactive_finalize_failure_uses_partial_cleanup(
    tmp_path: Path,
    monkeypatch,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    dest = tmp_path / f"backups/host/{BETA_UUID}/2026-05/stamp"
    cleaned: list[Path] = []
    monkeypatch.setattr("libvirt_backup_system.backup.domain_state", lambda cfg, name: "running")
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            dest.mkdir(parents=True, exist_ok=True),
            (dest / "vda.copy.data").write_bytes(b"x"),
            CommandResult(args, 0, "", ""),
        )[2],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup._attempt_partial_cleanup",
        lambda config, path, vm_name: cleaned.append(path),
    )

    assert not backup_vm(cfg, VM("beta", "shut off", BETA_UUID), "2026-05", "stamp")
    assert cleaned == [dest]
