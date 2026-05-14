from __future__ import annotations

import json
from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.run_records import RUNS_FILE
from libvirt_backup_system.shell import CommandResult
from tests.unit.conftest import ALPHA_UUID


def _stamp(month: str, day: int, hour: int = 12) -> str:
    return f"{month.replace('-', '')}{day:02d}T{hour:02d}0000"


def _seed_chain(cfg: Config, chain: str) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    (vm_dir / "2026-05" / chain).mkdir(parents=True)
    return vm_dir / "2026-05" / chain


def _write_runs_jsonl(chain_dir: Path, records: list[tuple[str, str]]) -> None:
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / RUNS_FILE).write_text(
        "\n".join(json.dumps({"ts": ts, "checkpoint": cp}) for ts, cp in records) + "\n",
        encoding="utf-8",
    )


def _capture_run(monkeypatch) -> list[list[str]]:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )
    return captured


def _disable_mount(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_restore_at_passes_until_for_intermediate_run(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    # Chain starts May 1 with five recorded runs; --at May 3 12:00 must stop
    # at the May 3 12:00 checkpoint instead of replaying through May 5.
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(
        chain_dir,
        [
            ("20260501T080000", "virtnbdbackup.0"),
            ("20260502T080000", "virtnbdbackup.1"),
            ("20260503T120000", "virtnbdbackup.2"),
            ("20260504T080000", "virtnbdbackup.3"),
            ("20260505T080000", "virtnbdbackup.4"),
        ],
    )
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-03T12:00:00") == 0
    args = captured[0]
    assert args[args.index("-u") + 1] == "virtnbdbackup.2"


def test_restore_at_omits_until_when_target_is_at_or_after_last_run(
    tmp_path: Path, monkeypatch, backup_config: Config
) -> None:
    # --at at-or-after the last recorded run means "chain end": omit --until
    # so virtnbdrestore replays everything (matches no---at default behavior).
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(
        chain_dir,
        [("20260501T080000", "virtnbdbackup.0"), ("20260502T080000", "virtnbdbackup.1")],
    )
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-09T00:00:00") == 0
    assert "-u" not in captured[0]


def test_restore_at_legacy_chain_without_runs_jsonl_replays_chain_end(
    tmp_path: Path, monkeypatch, backup_config: Config
) -> None:
    # A chain dir from before this feature has no runs.jsonl: restore picks
    # the chain by start time but omits --until so the whole chain replays.
    cfg = _disable_mount(backup_config)
    _seed_chain(cfg, _stamp("2026-05", 1, 8))
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-03T12:00:00") == 0
    assert "-u" not in captured[0]


def test_restore_without_at_never_passes_until(tmp_path: Path, monkeypatch, backup_config: Config) -> None:
    # Even when runs.jsonl exists, omitting --at means "latest snapshot":
    # restore must not stop early at the first recorded checkpoint.
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(
        chain_dir,
        [("20260501T080000", "virtnbdbackup.0"), ("20260505T080000", "virtnbdbackup.4")],
    )
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 0
    assert "-u" not in captured[0]
