from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import verify
from libvirt_backup_system.config import Config
from libvirt_backup_system.restore import restore
from libvirt_backup_system.shell import CommandResult
from tests.unit.conftest import ALPHA_UUID


def _seed_chain(cfg: Config, month: str, chain: str) -> Path:
    vm_dir = cfg.path_value("BACKUP_PATH") / cfg.get("HOST_ID") / ALPHA_UUID
    chain_dir = vm_dir / month / chain
    chain_dir.mkdir(parents=True)
    (chain_dir / "vda.full.data").write_bytes(b"x")
    return vm_dir


def test_restore_reports_missing_virtnbdrestore(tmp_path: Path, monkeypatch, backup_config: Config, capsys) -> None:
    # On a recovery host an operator may have skipped ``check``. Popen would
    # raise FileNotFoundError, which must surface as a clean operator error
    # rather than the cli's generic fatal-traceback path.
    backup_config.values["BACKUP_REQUIRE_NFS_MOUNT"] = "false"
    _seed_chain(backup_config, "2026-01", "20260105T120000")

    def missing(args: list[str]) -> CommandResult:
        raise FileNotFoundError(2, "No such file or directory: 'virtnbdrestore'")

    monkeypatch.setattr("libvirt_backup_system.restore.run_streamed", missing)
    assert restore(backup_config, ALPHA_UUID, tmp_path / "out") == 1
    err = capsys.readouterr().err
    assert "restore failed: virtnbdrestore unavailable" in err
    assert "Traceback" not in err


def test_verify_reports_missing_virtnbdrestore(tmp_path: Path, monkeypatch, capsys, backup_config) -> None:
    # Same recovery-host concern as restore: Popen raising FileNotFoundError
    # must surface as a clean operator error.
    (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/good").mkdir(parents=True)

    def missing(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise FileNotFoundError(2, "No such file or directory: 'virtnbdrestore'")

    monkeypatch.setattr("libvirt_backup_system.verify.run_streamed", missing)
    assert verify(backup_config) == 1
    err = capsys.readouterr().err
    assert "verify failed: virtnbdrestore unavailable" in err
    assert "Traceback" not in err
