from __future__ import annotations

import json
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.shell import CommandError, CommandResult
from tests.unit.conftest import ALPHA_UUID

ALPHA_NAME = "alpha"


def _seed_chain(cfg: Config, months_and_chains: dict[str, list[str]], *, host_id: str | None = None) -> Path:
    host = host_id or cfg.get("HOST_ID")
    vm_dir = cfg.path_value("BACKUP_PATH") / host / ALPHA_UUID
    vm_dir.mkdir(parents=True, exist_ok=True)
    for month, chains in months_and_chains.items():
        (vm_dir / month).mkdir(exist_ok=True)
        for chain in chains:
            chain_dir = vm_dir / month / chain
            chain_dir.mkdir()
            (chain_dir / "vda.full.data").write_bytes(b"x")
            checkpoint = f"virtnbdbackup.{chain}"
            record = json.dumps({"ts": chain, "checkpoint": checkpoint}, sort_keys=True, separators=(",", ":"))
            (chain_dir / "runs.jsonl").write_text(record + "\n", encoding="utf-8")
            (chain_dir / "metadata.json").write_text(json.dumps({"domain": ALPHA_NAME}), encoding="utf-8")
    return vm_dir


def _restore_config(cfg: Config, tmp_path: Path) -> Config:
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    cfg.values["HOST_ID"] = "host"
    cfg.values["LIBVIRT_BACKUP_ROOT_PREFIX"] = str(tmp_path)
    return cfg


def _stamp(month: str, day: int, hour: int = 12) -> str:
    return f"{month.replace('-', '')}{day:02d}T{hour:02d}0000"


def _stub_no_local_vm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the turnkey path: ``virsh domname`` reports no local match."""
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run",
        lambda args, **kwargs: (_ for _ in ()).throw(
            CommandError(CommandResult(args, 1, "", "no domain with matching name 'x'"))
        ),
    )


def _capture_streamed(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: captured.append(args) or CommandResult(args, 0, "", ""),
    )
    return captured


def test_restore_turnkey_when_no_local_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    # Backup belongs to this host but libvirt has no domain with that UUID:
    # restore falls through to the turnkey define path rather than refusing.
    cfg = _restore_config(backup_config, tmp_path)
    stamp = _stamp("2026-05", 7, 10)
    _seed_chain(cfg, {"2026-05": [stamp]})
    _stub_no_local_vm(monkeypatch)
    captured = _capture_streamed(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 0
    cmd = captured[0]
    assert cmd[:3] == ["virtnbdrestore", "-a", "restore"]
    assert "-D" in cmd  # turnkey mode redefines the VM
    assert cmd[cmd.index("-u") + 1] == f"virtnbdbackup.{stamp}"


def test_restore_overwrite_when_local_vm_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    # Same host AND libvirt has the VM: restore shuts the VM off, undefines it,
    # then redefines from the backup. The sequence is asserted on the order of
    # virsh subcommands captured.
    cfg = _restore_config(backup_config, tmp_path)
    stamp = _stamp("2026-05", 7, 10)
    _seed_chain(cfg, {"2026-05": [stamp]})
    virsh_calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        virsh_calls.append(args)
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "domstate" in args:
            return CommandResult(args, 0, "shut off", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    captured = _capture_streamed(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 0
    actions = [tuple(call[1:5]) for call in virsh_calls]
    assert ("-c", cfg.get("LIBVIRT_URI"), "destroy", "--") in actions
    assert any("undefine" in call for call in virsh_calls)
    assert captured[0][:3] == ["virtnbdrestore", "-a", "restore"]


def test_restore_overwrite_refuses_if_destroy_leaves_vm_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    stamp = _stamp("2026-05", 7, 10)
    _seed_chain(cfg, {"2026-05": [stamp]})

    def fake_run(args: list[str], **_kwargs: object) -> CommandResult:
        if "domname" in args:
            return CommandResult(args, 0, ALPHA_NAME, "")
        if "destroy" in args:
            return CommandResult(args, 0, "", "")
        if "domstate" in args:
            return CommandResult(args, 0, "running", "")
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.restore.run", fake_run)
    monkeypatch.setattr(
        "libvirt_backup_system.restore.run_streamed",
        lambda args: (_ for _ in ()).throw(AssertionError("must not run when VM still up")),
    )

    assert restore(cfg, ALPHA_UUID, stamp) == 1
    assert "refusing to overwrite" in capsys.readouterr().err


def test_restore_uses_cross_host_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config) -> None:
    # Backup lives under a different host_id than this machine's; restore must
    # still find it (cross-host recovery) and pick the turnkey define path.
    cfg = _restore_config(backup_config, tmp_path)
    stamp = _stamp("2026-05", 7, 10)
    _seed_chain(cfg, {"2026-05": [stamp]}, host_id="other-host")
    _stub_no_local_vm(monkeypatch)
    captured = _capture_streamed(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 0
    chain_dir = cfg.path_value("BACKUP_PATH") / "other-host" / ALPHA_UUID / "2026-05" / stamp
    assert captured[0][captured[0].index("-i") + 1] == str(chain_dir)


def test_restore_rejects_unknown_timestamp(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    _seed_chain(cfg, {"2026-05": [_stamp("2026-05", 7, 10)]})

    assert restore(cfg, ALPHA_UUID, _stamp("2026-05", 8)) == 1
    assert "no backup matching uuid and timestamp" in capsys.readouterr().err


def test_restore_rejects_malformed_timestamp(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    _seed_chain(cfg, {"2026-05": [_stamp("2026-05", 7, 10)]})

    assert restore(cfg, ALPHA_UUID, "2026-05-07") == 1
    assert "timestamp is malformed" in capsys.readouterr().err


def test_restore_rejects_invalid_uuid(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    assert restore(cfg, "not-a-uuid", _stamp("2026-05", 7, 10)) == 1
    assert "not a valid UUID" in capsys.readouterr().err


def test_restore_legacy_chain_without_runs_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backup_config: Config
) -> None:
    # A legacy chain (no runs.jsonl) is identified by its chain_id; restore
    # uses chain-end semantics (no --until in the virtnbdrestore command).
    cfg = _restore_config(backup_config, tmp_path)
    stamp = _stamp("2026-05", 7, 10)
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    chain_dir = vm_dir / "2026-05" / stamp
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    _stub_no_local_vm(monkeypatch)
    captured = _capture_streamed(monkeypatch)

    assert restore(cfg, ALPHA_UUID, stamp) == 0
    assert "-u" not in captured[0]


def test_restore_refuses_when_mount_missing(
    tmp_path: Path, backup_config: Config, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _restore_config(backup_config, tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    _seed_chain(cfg, {"2026-05": [_stamp("2026-05", 7, 10)]})
    assert restore(cfg, ALPHA_UUID, _stamp("2026-05", 7, 10)) == 1
    assert "BACKUP_PATH is no longer a mount point" in capsys.readouterr().err
