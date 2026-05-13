from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID


def _backup_config(cfg: Config) -> Config:
    cfg.values.update({"BACKUP_COMPRESS": "true", "INACTIVE_COPY_EVERY_RUN": "false"})
    return cfg


def test_backup_vm_running_second_run_is_incremental(tmp_path: Path, monkeypatch, backup_config) -> None:
    # The first run writes a chain pointer; the second run with an unchanged
    # fingerprint must reuse that chain and invoke ``-l inc`` against the same
    # dest dir rather than starting a new full.
    cfg = _backup_config(backup_config)
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-a")
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-b")
    dest = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-a"
    assert calls[0][calls[0].index("-l") + 1] == "full"
    assert calls[0][calls[0].index("-o") + 1] == str(dest)
    assert calls[1][calls[1].index("-l") + 1] == "inc"
    assert calls[1][calls[1].index("-o") + 1] == str(dest)


def test_backup_vm_running_fingerprint_change_starts_new_chain(tmp_path: Path, monkeypatch, backup_config) -> None:
    # Domain XML edits between runs invalidate the existing chain (a fingerprint
    # mismatch makes any later increment unrestorable). Verify the run starts
    # a fresh full into a new chain dir.
    cfg = _backup_config(backup_config)
    fingerprints = iter(["fp-a", "fp-b"])
    monkeypatch.setattr(
        "libvirt_backup_system.backup.domain_xml_fingerprint",
        lambda uri, name: next(fingerprints),
    )
    calls: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-a")
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-b")
    chain_a = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-a"
    chain_b = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-b"
    assert calls[0][calls[0].index("-l") + 1] == "full"
    assert calls[0][calls[0].index("-o") + 1] == str(chain_a)
    assert calls[1][calls[1].index("-l") + 1] == "full"
    assert calls[1][calls[1].index("-o") + 1] == str(chain_b)


def test_backup_vm_running_fails_when_pre_fingerprint_unavailable(monkeypatch, capsys, backup_config) -> None:
    # Running VMs also need a domain-XML fingerprint to drive chain selection.
    # virsh returning None must surface as an error and abort the VM, not
    # silently start a brand-new chain on every run.
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: None)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "domain XML fingerprint computation failed" in capsys.readouterr().err


def test_backup_vm_running_fails_when_chain_state_write_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # A successful virtnbdbackup that cannot persist its chain pointer must
    # fail the VM: the next run would otherwise see "no chain" and start a
    # fresh full into a different chain dir, orphaning the data we just wrote.
    cfg = _backup_config(backup_config)
    monkeypatch.setattr(
        "libvirt_backup_system.backup.run_streamed",
        lambda args, check=True, env=None: (
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True),
            CommandResult(args, 0, "", ""),
        )[1],
    )
    monkeypatch.setattr(
        "libvirt_backup_system.backup.write_chain_state",
        lambda month_dir, chain_id, fingerprint, vm: False,
    )

    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    capsys.readouterr()


def test_backup_vm_incremental_failure_preserves_chain_dir(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    # An incremental failure must never wipe the chain dir: the prior full +
    # earlier increments are still valuable. Only new fulls own the chain dir
    # end-to-end and are cleanable on failure.
    cfg = _backup_config(backup_config)
    chain_dir = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-a"
    seen: list[str] = []

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        level = args[args.index("-l") + 1]
        seen.append(level)
        if level == "full":
            Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
            return CommandResult(args, 0, "", "")
        raise CommandError(CommandResult(args, 9, "", "incremental boom"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-a")
    (chain_dir / "fixture").write_bytes(b"keep-me")
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-b")
    assert chain_dir.is_dir()
    assert (chain_dir / "fixture").exists()
    err = capsys.readouterr().err
    assert "backup failed" in err
    assert "removed partial backup" not in err
    assert seen == ["full", "inc"]
