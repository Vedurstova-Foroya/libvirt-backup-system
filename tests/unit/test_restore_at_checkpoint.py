from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.run_records import RUNS_FILE, poison_chain
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID


def _stamp(month: str, day: int, hour: int = 12) -> str:
    return f"{month.replace('-', '')}{day:02d}T{hour:02d}0000"


def _seed_chain(cfg: Config, chain: str) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    chain_dir = vm_dir / "2026-05" / chain
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    return chain_dir


def _write_runs_jsonl(chain_dir: Path, records: list[tuple[str, str]]) -> None:
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / RUNS_FILE).write_text(
        "\n".join(json.dumps({"ts": ts, "checkpoint": cp}) for ts, cp in records) + "\n",
        encoding="utf-8",
    )


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )
    monkeypatch.setattr("libvirt_backup_system.restore.define_restored_domain", lambda *_args: True)
    return captured


def _stub_no_local_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run",
        lambda args, **kwargs: (_ for _ in ()).throw(CommandError(CommandResult(args, 1, "", "no domain"))),
    )


def _disable_mount(cfg: Config) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    return cfg


def test_restore_passes_until_for_intermediate_run(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # Selecting an intermediate run by its exact timestamp must compose into
    # ``virtnbdrestore --until <checkpoint>`` so the replay stops there.
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
    _stub_no_local_vm(monkeypatch)
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, "20260503T120000") == 0
    args = captured[0]
    assert args[args.index("-u") + 1] == "virtnbdbackup.2"


def test_restore_refuses_when_timestamp_not_in_runs(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    # An exact-timestamp restore must refuse when no run record matches: the
    # operator copied the stamp from somewhere it isn't actually recorded.
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(chain_dir, [("20260501T080000", "virtnbdbackup.0")])
    _stub_no_local_vm(monkeypatch)
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, "20260503T120000") == 1
    assert captured == []
    assert "no backup matching uuid and timestamp" in capsys.readouterr().err


def test_restore_legacy_chain_omits_until(monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # A chain pre-dating runs.jsonl is identified by its chain_id; restore
    # uses chain-end semantics (no ``--until`` argument).
    cfg = _disable_mount(backup_config)
    stamp = _stamp("2026-05", 1, 8)
    _seed_chain(cfg, stamp)
    _stub_no_local_vm(monkeypatch)
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 0
    assert "-u" not in captured[0]


def test_restore_refuses_poisoned_legacy_chain(
    monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _disable_mount(backup_config)
    stamp = _stamp("2026-05", 1, 8)
    chain_dir = _seed_chain(cfg, stamp)
    assert poison_chain(chain_dir, "alpha", "record_run failed")
    _stub_no_local_vm(monkeypatch)
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert captured == []
    assert "restore refused poisoned chain" in capsys.readouterr().err
