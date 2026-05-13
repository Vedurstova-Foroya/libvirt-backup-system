from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.chains import (
    CHAIN_FINGERPRINT_NAME,
    CHAIN_POINTER_NAME,
    read_chain_state,
    resolve_chain,
    write_chain_state,
)
from libvirt_backup_system.config import Config


def _cfg(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_read_chain_state_missing_files(tmp_path: Path) -> None:
    assert read_chain_state(tmp_path) == (None, None)


def test_write_then_read_chain_state(tmp_path: Path) -> None:
    assert write_chain_state(tmp_path, "stamp-a", "fp-a", "alpha")
    assert read_chain_state(tmp_path) == ("stamp-a", "fp-a")


def test_read_chain_state_treats_empty_files_as_missing(tmp_path: Path) -> None:
    (tmp_path / CHAIN_POINTER_NAME).write_text("\n", encoding="utf-8")
    (tmp_path / CHAIN_FINGERPRINT_NAME).write_text("", encoding="utf-8")
    # An empty file from a partial prior write must read as "no chain" so the
    # next run forces a fresh full rather than treating an empty pointer as a
    # valid (but invalid) chain id.
    assert read_chain_state(tmp_path) == (None, None)


def test_read_chain_state_handles_read_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    pointer = tmp_path / CHAIN_POINTER_NAME
    pointer.write_text("stamp\n", encoding="utf-8")
    (tmp_path / CHAIN_FINGERPRINT_NAME).write_text("fp\n", encoding="utf-8")
    original = Path.read_text

    def fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == pointer:
            raise OSError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.chains.Path.read_text", fake_read_text)
    chain, fp = read_chain_state(tmp_path)
    assert chain is None
    assert fp == "fp"
    assert "chain state read failed" in capsys.readouterr().err


def test_resolve_chain_starts_new_when_no_state(tmp_path: Path, backup_config) -> None:
    cfg = _cfg(backup_config)
    month_dir = tmp_path / "month"
    month_dir.mkdir()
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp", "fp")
    assert resolution.is_new_chain
    assert resolution.level == "full"
    assert resolution.chain_dir == month_dir / "stamp"


def test_resolve_chain_reuses_existing_chain(tmp_path: Path, backup_config) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    (month_dir / "stamp-a").mkdir()
    assert write_chain_state(month_dir, "stamp-a", "fp", "alpha")
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert not resolution.is_new_chain
    assert resolution.level == "inc"
    assert resolution.chain_dir == month_dir / "stamp-a"


def test_resolve_chain_starts_new_on_fingerprint_change(tmp_path: Path, backup_config, capsys) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    (month_dir / "stamp-a").mkdir()
    assert write_chain_state(month_dir, "stamp-a", "fp-old", "alpha")
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp-new")
    assert resolution.is_new_chain
    assert resolution.level == "full"
    assert resolution.chain_dir == month_dir / "stamp-b"
    assert "domain XML fingerprint changed" in capsys.readouterr().out


def test_resolve_chain_starts_new_when_pointer_dir_missing(tmp_path: Path, backup_config, capsys) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    assert write_chain_state(month_dir, "stamp-a", "fp", "alpha")
    # Pointer references a directory that doesn't exist (operator pruned it,
    # crash mid-rename, etc.). Starting a new chain is the safe recovery.
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert resolution.is_new_chain
    assert resolution.chain_dir == month_dir / "stamp-b"
    assert "previous chain dir missing" in capsys.readouterr().out


def test_resolve_chain_rejects_unsafe_pointer(tmp_path: Path, backup_config, capsys) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    # Bypass write_chain_state (which would refuse the unsafe stamp at write
    # time) to simulate an operator-edited or upgrade-corrupted pointer file.
    (month_dir / "stamp-a").mkdir()
    (month_dir / ".current-chain").write_text("../../escape\n", encoding="utf-8")
    (month_dir / ".chain-fingerprint").write_text("fp\n", encoding="utf-8")
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert resolution.is_new_chain
    assert resolution.chain_dir == month_dir / "stamp-b"
    err = capsys.readouterr().err
    assert "chain pointer is unsafe" in err


def test_resolve_chain_rejects_chain_dir_outside_backup_path(tmp_path: Path, backup_config, capsys) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (month_dir / "stamp-a").symlink_to(outside, target_is_directory=True)
    (month_dir / ".current-chain").write_text("stamp-a\n", encoding="utf-8")
    (month_dir / ".chain-fingerprint").write_text("fp\n", encoding="utf-8")
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert resolution.is_new_chain
    assert "chain dir path is unsafe" in capsys.readouterr().err


def test_resolve_chain_starts_new_when_is_dir_raises(tmp_path: Path, backup_config, monkeypatch, capsys) -> None:
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    (month_dir / "stamp-a").mkdir()
    assert write_chain_state(month_dir, "stamp-a", "fp", "alpha")
    original_is_dir = Path.is_dir

    def fake(self: Path) -> bool:
        if self == month_dir / "stamp-a":
            raise OSError("denied")
        return original_is_dir(self)

    monkeypatch.setattr("libvirt_backup_system.chains.Path.is_dir", fake)
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert resolution.is_new_chain
    assert "chain dir check failed" in capsys.readouterr().err


def test_resolve_chain_rejects_pointer_when_backup_path_empty(tmp_path: Path, backup_config, capsys) -> None:
    # An empty BACKUP_PATH must make the safety check fail closed before the
    # pointer is followed; the resolution falls back to a new chain.
    cfg = _cfg(backup_config)
    month_dir = cfg.path_value("BACKUP_PATH") / "host" / "vm" / "2026-05"
    month_dir.mkdir(parents=True)
    (month_dir / "stamp-a").mkdir()
    assert write_chain_state(month_dir, "stamp-a", "fp", "alpha")
    cfg.values["BACKUP_PATH"] = ""
    resolution = resolve_chain(cfg, "alpha", month_dir, "stamp-b", "fp")
    assert resolution.is_new_chain
    assert "chain dir path is unsafe" in capsys.readouterr().err


def test_write_chain_state_pointer_failure_short_circuits(tmp_path: Path, monkeypatch, capsys) -> None:
    def fail_open(path: Path) -> int:
        raise OSError("denied")

    monkeypatch.setattr("libvirt_backup_system.inactive_markers._open_excl_nofollow", fail_open)
    assert not write_chain_state(tmp_path, "stamp", "fp", "alpha")
    err = capsys.readouterr().err
    assert "chain pointer write failed" in err
    # Fingerprint write must not have been attempted after pointer failed; only
    # one error event is expected.
    assert "chain fingerprint write failed" not in err
