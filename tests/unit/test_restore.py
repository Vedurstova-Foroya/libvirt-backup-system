from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def _seed_chain(cfg: Config, months_and_chains: dict[str, list[str]]) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    vm_dir.mkdir(parents=True, exist_ok=True)
    for month, chains in months_and_chains.items():
        (vm_dir / month).mkdir(exist_ok=True)
        for chain in chains:
            (vm_dir / month / chain).mkdir()
    return vm_dir


def _restore_config(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_restore_picks_latest_month_and_chain(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2025-12": ["a", "b"], "2026-01": ["c", "d"]})
    captured: list[list[str]] = []

    def fake_run(args: list[str]) -> CommandResult:
        captured.append(args)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fake_run)
    output = tmp_path / "out"
    assert restore(cfg, ALPHA_UUID, output) == 0
    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2026-01" / "d"
    assert captured == [["virtnbdrestore", "-a", "restore", "-i", str(expected), "-o", str(output)]]
    assert output.is_dir()


def test_restore_rejects_existing_non_empty_output(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    output = tmp_path / "out"
    output.mkdir()
    (output / "leftover").write_bytes(b"x")
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is not empty" in capsys.readouterr().err


def test_restore_allows_empty_existing_output(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: CommandResult(args, 0, "", ""),
    )
    assert restore(cfg, ALPHA_UUID, output) == 0


def test_restore_invalid_vm_name(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    assert restore(cfg, "../escape", tmp_path / "out") == 1
    assert "restore target name is invalid" in capsys.readouterr().err


def test_restore_missing_vm(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    cfg.path_value("BACKUP_PATH").mkdir()
    (cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")).mkdir()
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: None)
    assert restore(cfg, "missing", tmp_path / "out") == 1
    assert "restore target not found" in capsys.readouterr().err


def test_restore_resolved_uuid_dir_missing(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # ``resolve_vm_uuid`` returns a UUID but no backups have been written for
    # that UUID yet (operator restored from a fresh machine, etc.).
    cfg = _restore_config(backup_config)
    (cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")).mkdir(parents=True)
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: ALPHA_UUID)
    assert restore(cfg, "alpha", tmp_path / "out") == 1
    assert "restore target not found" in capsys.readouterr().err


def test_restore_resolves_vm_name_via_virsh(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    monkeypatch.setattr("libvirt_backup_system.restore.resolve_vm_uuid", lambda c, n: ALPHA_UUID)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: CommandResult(args, 0, "", ""),
    )
    assert restore(cfg, "alpha", tmp_path / "out") == 0


def test_restore_specific_month_and_chain(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2025-12": ["old"], "2026-01": ["c", "d"]})
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (captured.append(args), CommandResult(args, 0, "", ""))[1],
    )
    output = tmp_path / "out"
    assert restore(cfg, ALPHA_UUID, output, month="2025-12", chain="old") == 0
    expected = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID / "2025-12" / "old"
    assert captured[0][captured[0].index("-i") + 1] == str(expected)


def test_restore_specific_month_missing(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", month="2025-12") == 1
    assert "restore --month not found" in capsys.readouterr().err


def test_restore_malformed_month(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", month="bogus") == 1
    assert "restore --month is malformed" in capsys.readouterr().err


def test_restore_no_months_available(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    vm_dir.mkdir(parents=True)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore found no monthly backups" in capsys.readouterr().err


def test_restore_no_chains_in_month(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    (vm_dir / "2026-01").mkdir(parents=True)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore found no backups in month" in capsys.readouterr().err


def test_restore_unsafe_chain(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", chain="..") == 1
    assert "restore --chain is unsafe" in capsys.readouterr().err


def test_restore_missing_chain(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out", chain="other") == 1
    assert "restore --chain not found" in capsys.readouterr().err


def test_restore_command_failure(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})

    def fail(args: list[str]) -> CommandResult:
        raise CommandError(CommandResult(args, 7, "", "bad"))

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", fail)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore failed" in capsys.readouterr().err


def test_restore_refuses_when_mount_missing(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    _seed_chain(cfg, {"2026-01": ["c"]})
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err


def test_restore_refuses_unsafe_backup_root(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", lambda root, path: False)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because backup root is unsafe" in capsys.readouterr().err


def test_restore_output_creation_failure(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})

    def fail(self: Path, *args: object, **kwargs: object) -> None:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.restore.Path.mkdir", fail)
    assert restore(cfg, ALPHA_UUID, tmp_path / "deep" / "out") == 1
    assert "restore output directory creation failed" in capsys.readouterr().err


def test_restore_output_not_a_directory(tmp_path: Path, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    _seed_chain(cfg, {"2026-01": ["c"]})
    output = tmp_path / "file-not-dir"
    output.write_bytes(b"data")
    assert restore(cfg, ALPHA_UUID, output) == 1
    assert "restore output is not a usable directory" in capsys.readouterr().err


def test_restore_subpath_safety_per_level(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = _seed_chain(cfg, {"2026-01": ["c"]})
    month_dir = vm_dir / "2026-01"
    real_paths_safe = {cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID"), vm_dir}

    def fake_safe(root: Path, path: Path) -> bool:
        return path in real_paths_safe

    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", fake_safe)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because month path is unsafe" in capsys.readouterr().err
    real_paths_safe.add(month_dir)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out2") == 1
    assert "restore skipped because chain path is unsafe" in capsys.readouterr().err


def test_restore_vm_root_path_unsafe(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _restore_config(backup_config)
    vm_dir = _seed_chain(cfg, {"2026-01": ["c"]})
    host_root = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID")

    def fake_safe(root: Path, path: Path) -> bool:
        return path == host_root

    monkeypatch.setattr("libvirt_backup_system.restore.subpath_is_safe", fake_safe)
    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert "restore skipped because VM root is unsafe" in capsys.readouterr().err
    # vm_dir reference kept to silence linter about unused local.
    assert vm_dir.is_dir()
