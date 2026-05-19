from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.retention import _has_full_backup, prune_old_months
from tests.unit.conftest import ALPHA_UUID

_TWELVE_MONTHS = [
    "2025-05",
    "2025-06",
    "2025-07",
    "2025-08",
    "2025-09",
    "2025-10",
    "2025-11",
    "2025-12",
    "2026-01",
    "2026-02",
    "2026-03",
    "2026-04",
]


def _vm_dir(cfg: Config, vm_uuid: str) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / vm_uuid
    vm_dir.mkdir(parents=True, exist_ok=True)
    return vm_dir


def _full(month_dir: Path) -> Path:
    """Create a chain dir containing a virtnbdbackup ``-l full`` data file —
    the retention gate counts only chain dirs with a restore-standalone full
    (or ``-l copy``) data file, not incrementals."""
    chain = month_dir / "20260101T000000"
    chain.mkdir(parents=True, exist_ok=True)
    (chain / "vda.full.data").write_bytes(b"x")
    return chain


def _seed_full(cfg: Config, vm_uuid: str, months: list[str]) -> Path:
    vm_dir = _vm_dir(cfg, vm_uuid)
    for month in months:
        (vm_dir / month).mkdir()
        _full(vm_dir / month)
    return vm_dir


def _enable(cfg: Config, months: int) -> Config:
    cfg.values["BACKUP_RETENTION_MONTHS"] = str(months)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_prune_skipped_until_current_month_has_full_backup(backup_config: Config, capsys) -> None:
    # Gate 1: with retention=12 and 13 months on disk, the oldest is only
    # dropped after the current month holds its own full backup. An empty
    # current-month dir — or one with only an inc data file from a manual
    # repair — must not release the prune.
    cfg = _enable(backup_config, 12)
    vm_dir = _seed_full(cfg, ALPHA_UUID, ["2025-04", *_TWELVE_MONTHS])
    inc_only = vm_dir / "2026-05" / "20260501T000000"
    inc_only.mkdir(parents=True)
    (inc_only / "vda.inc.virtnbdbackup.1.data").write_bytes(b"x")
    assert prune_old_months(cfg, current_month="2026-05") == 0
    assert "retention skipped for VM without full backup in current month" in capsys.readouterr().out
    assert "2025-04" in {p.name for p in vm_dir.iterdir()}
    # Adding a -l full data file to the chain lifts the gate.
    (inc_only / "vda.full.data").write_bytes(b"x")
    assert prune_old_months(cfg, current_month="2026-05") == 0
    assert "2025-04" not in {p.name for p in vm_dir.iterdir()}


def test_prune_skipped_when_months_with_full_count_at_floor(backup_config: Config, capsys) -> None:
    # Gate 2: with retention=12, prune is held until at least 13 months *each*
    # hold their own full backup. 11 prior months + current month = 12 months
    # with a full, plus an older month dir whose chain has only inc data —
    # the inc-only month must NOT count toward the floor.
    cfg = _enable(backup_config, 12)
    vm_dir = _seed_full(cfg, ALPHA_UUID, [*_TWELVE_MONTHS[1:], "2026-05"])
    inc_chain = vm_dir / "2025-04" / "20250401T000000"
    inc_chain.mkdir(parents=True)
    (inc_chain / "vda.inc.virtnbdbackup.1.data").write_bytes(b"x")
    assert prune_old_months(cfg, current_month="2026-05") == 0
    assert "retention skipped because months-with-full-backup count" in capsys.readouterr().out
    assert (vm_dir / "2025-04").is_dir()
    # Promote the inc-only chain to a full → 13 months with a full → prune fires.
    (inc_chain / "vda.full.data").write_bytes(b"x")
    assert prune_old_months(cfg, current_month="2026-05") == 0
    assert "2025-04" not in {p.name for p in vm_dir.iterdir()}


def test_prune_preserves_full_backup_floor_with_recent_incremental_only_month(backup_config: Config) -> None:
    # Four full-bearing months and one recent inc-only month with retention=3
    # should prune only one full-bearing month. Pruning by total month dirs
    # would delete two full-bearing months and leave only two restorable months.
    cfg = _enable(backup_config, 3)
    vm_dir = _seed_full(cfg, ALPHA_UUID, ["2026-01", "2026-02", "2026-03", "2026-05"])
    inc_chain = vm_dir / "2026-04" / "20260401T000000"
    inc_chain.mkdir(parents=True)
    (inc_chain / "vda.inc.virtnbdbackup.1.data").write_bytes(b"x")

    assert prune_old_months(cfg, current_month="2026-05") == 0

    assert sorted(p.name for p in vm_dir.iterdir()) == ["2026-02", "2026-03", "2026-04", "2026-05"]
    assert sum(1 for month in vm_dir.iterdir() if _has_full_backup(month)) == 3


def test_copy_data_files_count_as_full_for_inactive_vms(backup_config: Config) -> None:
    # Inactive VMs use ``virtnbdbackup -l copy`` which produces
    # ``<vm>.<disk>.copy.data``; that file is restore-standalone so it
    # satisfies the full-backup gate the same way ``.full.`` does.
    cfg = _enable(backup_config, 1)
    vm_dir = _vm_dir(cfg, ALPHA_UUID)
    (vm_dir / "2025-12").mkdir()
    chain = vm_dir / "2025-12" / "20251201T000000"
    chain.mkdir(parents=True)
    (chain / "vda.copy.data").write_bytes(b"x")
    (vm_dir / "2026-01").mkdir()
    cur = vm_dir / "2026-01" / "20260101T000000"
    cur.mkdir(parents=True)
    (cur / "vda.copy.data").write_bytes(b"x")
    assert prune_old_months(cfg, current_month="2026-01") == 0
    assert {p.name for p in vm_dir.iterdir()} == {"2026-01"}


def test_has_full_backup_handles_unreadable_or_non_dir_entries(tmp_path: Path, monkeypatch) -> None:
    # iterdir on the month dir raising → False.
    monkeypatch.setattr(Path, "iterdir", lambda self: (_ for _ in ()).throw(OSError("denied")))
    assert _has_full_backup(tmp_path / "missing") is False
    monkeypatch.undo()

    # A stray file at the month-dir level isn't a chain; an inc-only chain
    # also doesn't count. Both branches return False until a full lands.
    month = tmp_path / "month"
    month.mkdir()
    (month / "stray.txt").write_text("not-a-chain", encoding="utf-8")
    chain = month / "20260101T000000"
    chain.mkdir()
    # Non-data entries inside a chain dir (metadata.json, the inc data file,
    # and a sub-directory) must all be skipped without confusing the scan.
    (chain / "metadata.json").write_text("{}", encoding="utf-8")
    (chain / "checkpoints").mkdir()
    (chain / "vda.inc.virtnbdbackup.1.data").write_bytes(b"x")
    assert _has_full_backup(month) is False
    (chain / "vda.full.data").write_bytes(b"x")
    assert _has_full_backup(month) is True

    # Chain dir whose iterdir fails: silently skipped, not raised.
    bad_month = tmp_path / "bad-month"
    bad_chain = bad_month / "20260101T000000"
    bad_chain.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def selective_fail(self: Path) -> object:
        if self == bad_chain:
            raise OSError("denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", selective_fail)
    assert _has_full_backup(bad_month) is False
