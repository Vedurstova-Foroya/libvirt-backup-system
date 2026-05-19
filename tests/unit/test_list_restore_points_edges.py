"""Edge-case tests for ``list_restore_points.enumerate_backups`` defensive paths.

Filesystem walks fall through to ``OSError`` on permission denied or NFS
hiccup, corrupt JSON in metadata.json / runs.jsonl must not crash the listing,
and ``subpath_is_safe`` must refuse any operator-dropped symlinks under the
backup tree. These tests exercise each branch with monkeypatched fakes so the
coverage gate does not regress when the module is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.list_restore_points import enumerate_backups, list_restore_points
from tests.unit.conftest import ALPHA_UUID


def _no_mount(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def _seed_minimal_chain(cfg: Config) -> Path:
    chain_dir = cfg.path_value("BACKUP_PATH") / "host" / ALPHA_UUID / "2026-05" / "20260501T080000"
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    return chain_dir


def test_enumerate_returns_empty_when_backup_path_unset(backup_config: Config) -> None:
    cfg = _no_mount(backup_config)
    cfg.values["BACKUP_PATH"] = ""
    assert enumerate_backups(cfg) == []


def test_enumerate_returns_empty_when_backup_path_missing(backup_config: Config) -> None:
    # Config points at a path that does not exist on disk yet (operator typo).
    cfg = _no_mount(backup_config)
    assert enumerate_backups(cfg) == []


def test_enumerate_recovers_vm_name_from_cpt_filename(backup_config: Config) -> None:
    # virtnbdbackup writes ``<vm>.cpt`` next to the data files; the listing
    # must read the VM name from that filename so operators see something
    # other than ``-`` in the VM_NAME column.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)
    (chain_dir / "alpha.cpt").write_text("[]", encoding="utf-8")
    rows = enumerate_backups(cfg)
    assert len(rows) == 1
    assert rows[0].vm_name == "alpha"


def test_enumerate_treats_missing_cpt_as_unknown_name(backup_config: Config) -> None:
    # A chain dir without any ``.cpt`` file (legacy chain, partial copy
    # interrupted before virtnbdbackup wrote its checkpoint state) surfaces
    # as empty VM name; the listing still renders the chain.
    cfg = _no_mount(backup_config)
    _seed_minimal_chain(cfg)
    rows = enumerate_backups(cfg)
    assert len(rows) == 1
    assert rows[0].vm_name == ""


def test_enumerate_rejects_unsafe_cpt_filename(backup_config: Config) -> None:
    # A .cpt filename whose stem fails the safe-name check (NUL/control char
    # planted by a hostile filesystem) must not flow into the displayed name.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)
    (chain_dir / "-evil.cpt").write_text("[]", encoding="utf-8")
    rows = enumerate_backups(cfg)
    assert rows[0].vm_name == ""


def test_enumerate_handles_oserror_listing_chain_dir(
    backup_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``_read_vm_name`` iterates the chain dir. A permission denied or NFS
    # hiccup on that one iterdir must not crash enumeration; the row still
    # surfaces with an empty VM name.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)
    real_iterdir = Path.iterdir

    def fail_for_chain(self: Path) -> object:
        if self == chain_dir:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr("libvirt_backup_system.list_restore_points.Path.iterdir", fail_for_chain)
    rows = enumerate_backups(cfg)
    assert len(rows) == 1
    assert rows[0].vm_name == ""


def test_enumerate_handles_per_entry_oserror_during_cpt_scan(
    backup_config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``entry.is_file()`` can raise on a broken symlink or a vanished entry
    # mid-walk. The .cpt scan must skip that entry instead of crashing the
    # whole enumeration.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)
    flaky = chain_dir / "flaky.cpt"
    flaky.write_text("[]", encoding="utf-8")
    real_is_file = Path.is_file

    def fail_for_flaky(self: Path) -> bool:
        if self == flaky:
            raise PermissionError("denied")
        return real_is_file(self)

    monkeypatch.setattr("libvirt_backup_system.list_restore_points.Path.is_file", fail_for_flaky)
    rows = enumerate_backups(cfg)
    # The flaky entry is skipped; no other .cpt exists, so the name is empty
    # but the row still renders.
    assert rows[0].vm_name == ""


def test_enumerate_runs_jsonl_skips_blank_and_corrupt_lines(backup_config: Config) -> None:
    # runs.jsonl with a mix of blank lines, malformed JSON, missing keys, and
    # empty values must drop the bad ones and keep the valid record.
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)
    runs_lines = [
        "",
        "not-json",
        '{"ts": "", "checkpoint": "virtnbdbackup.0"}',  # empty ts → dropped
        '{"checkpoint": "virtnbdbackup.0"}',  # missing ts → dropped
        '{"ts": "20260501T080000", "checkpoint": "virtnbdbackup.0"}',
    ]
    (chain_dir / "runs.jsonl").write_text("\n".join(runs_lines) + "\n", encoding="utf-8")
    rows = enumerate_backups(cfg)
    assert [r.timestamp for r in rows] == ["20260501T080000"]
    assert rows[0].checkpoint == "virtnbdbackup.0"


def test_enumerate_vm_iterdir_oserror_skips_vm(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # Permission denied listing a VM dir must skip that VM cleanly, not abort
    # the whole enumeration.
    cfg = _no_mount(backup_config)
    vm_dir = _seed_minimal_chain(cfg).parent.parent
    real_iterdir = Path.iterdir

    def fail_for_vm(self: Path) -> object:
        if self == vm_dir:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_for_vm)
    assert enumerate_backups(cfg) == []


def test_enumerate_month_iterdir_oserror_skips_month(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    cfg = _no_mount(backup_config)
    month_dir = _seed_minimal_chain(cfg).parent
    real_iterdir = Path.iterdir

    def fail_for_month(self: Path) -> object:
        if self == month_dir:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_for_month)
    assert enumerate_backups(cfg) == []


def test_enumerate_host_iterdir_oserror_skips_host_vms(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # iterdir on a host dir failing must drop that host's VMs from the walk.
    cfg = _no_mount(backup_config)
    host_dir = _seed_minimal_chain(cfg).parent.parent.parent
    real_iterdir = Path.iterdir

    def fail_for_host(self: Path) -> object:
        if self == host_dir:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_for_host)
    assert enumerate_backups(cfg) == []


def test_enumerate_backup_path_iterdir_oserror_returns_empty(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    cfg = _no_mount(backup_config)
    _seed_minimal_chain(cfg)
    backup_path = cfg.path_value("BACKUP_PATH")
    real_iterdir = Path.iterdir

    def fail_for_root(self: Path) -> object:
        if self == backup_path:
            raise PermissionError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fail_for_root)
    assert enumerate_backups(cfg) == []


def test_enumerate_rejects_unsafe_host_dir(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    cfg = _no_mount(backup_config)
    _seed_minimal_chain(cfg)
    monkeypatch.setattr("libvirt_backup_system.list_restore_points.subpath_is_safe", lambda _root, _path: False)
    assert enumerate_backups(cfg) == []


def test_enumerate_rejects_unsafe_vm_dir(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # Selectively reject only the vm_dir level: host_dir passes, vm_dir fails.
    cfg = _no_mount(backup_config)
    vm_dir = _seed_minimal_chain(cfg).parent.parent

    def selective(_root: Path, path: Path) -> bool:
        return path != vm_dir

    monkeypatch.setattr("libvirt_backup_system.list_restore_points.subpath_is_safe", selective)
    assert enumerate_backups(cfg) == []


def test_enumerate_rejects_unsafe_chain_dir(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    cfg = _no_mount(backup_config)
    chain_dir = _seed_minimal_chain(cfg)

    def selective(_root: Path, path: Path) -> bool:
        return path != chain_dir

    monkeypatch.setattr("libvirt_backup_system.list_restore_points.subpath_is_safe", selective)
    assert enumerate_backups(cfg) == []


def test_list_restore_points_refuses_when_mount_missing(
    backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = backup_config
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    cfg.path_value("BACKUP_PATH").mkdir()
    assert list_restore_points(cfg) == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err
