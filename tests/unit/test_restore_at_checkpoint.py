from __future__ import annotations

import json
from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.run_records import RUNS_FILE, poison_chain
from libvirt_backup_system.shell import CommandResult
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


def test_restore_at_refuses_when_runs_jsonl_missing(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    cfg = _disable_mount(backup_config)
    _seed_chain(cfg, _stamp("2026-05", 1, 8))
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-03T12:00:00") == 1
    assert captured == []
    assert "restore --at has no matching run record" in capsys.readouterr().err


def test_restore_at_refuses_when_runs_jsonl_has_no_matching_record(
    tmp_path: Path, monkeypatch, backup_config: Config, capsys
) -> None:
    # runs.jsonl exists but every record is later than --at (e.g. the leading
    # records were truncated by a power loss). Falling back to chain end would
    # silently restore a NEWER state than the operator asked for; restore must
    # refuse instead.
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(
        chain_dir,
        [("20260505T120000", "virtnbdbackup.4"), ("20260506T120000", "virtnbdbackup.5")],
    )
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-03T00:00:00") == 1
    assert captured == []
    assert "restore --at has no matching run record" in capsys.readouterr().err


def test_restore_at_refuses_when_runs_jsonl_is_empty(
    tmp_path: Path, monkeypatch, backup_config: Config, capsys
) -> None:
    # An empty / all-corrupt runs.jsonl means we cannot prove what's in this
    # chain.
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    (chain_dir / "runs.jsonl").write_text("not-json\n", encoding="utf-8")
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-03T00:00:00") == 1
    assert captured == []
    assert "restore --at has no matching run record" in capsys.readouterr().err


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


def test_restore_without_at_refuses_poisoned_chain_end(
    tmp_path: Path, monkeypatch, backup_config: Config, capsys
) -> None:
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(chain_dir, [("20260501T080000", "virtnbdbackup.0")])
    assert poison_chain(chain_dir, "alpha", "record_run failed")
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out") == 1
    assert captured == []
    assert "restore refused poisoned chain end" in capsys.readouterr().err


def test_restore_at_after_last_record_refuses_poisoned_chain_end(
    tmp_path: Path, monkeypatch, backup_config: Config, capsys
) -> None:
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(chain_dir, [("20260501T080000", "virtnbdbackup.0")])
    assert poison_chain(chain_dir, "alpha", "record_run failed")
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-02T00:00:00") == 1
    assert captured == []
    assert "restore --at would replay poisoned chain end" in capsys.readouterr().err


def test_restore_at_exact_last_record_on_poisoned_chain_uses_until(
    tmp_path: Path, monkeypatch, backup_config: Config
) -> None:
    cfg = _disable_mount(backup_config)
    chain_dir = _seed_chain(cfg, _stamp("2026-05", 1, 8))
    _write_runs_jsonl(chain_dir, [("20260501T080000", "virtnbdbackup.0")])
    assert poison_chain(chain_dir, "alpha", "record_run failed")
    captured = _capture_run(monkeypatch)

    assert restore(cfg, ALPHA_UUID, tmp_path / "out", at="2026-05-01T08:00:00") == 0
    assert captured[0][captured[0].index("-u") + 1] == "virtnbdbackup.0"
