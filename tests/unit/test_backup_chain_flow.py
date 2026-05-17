from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.backup import backup_vm
from libvirt_backup_system.chains import CHAIN_STATE_NAME
from libvirt_backup_system.config import Config
from libvirt_backup_system.run_records import CHAIN_POISON_NAME, CheckpointReadError
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import VM
from tests.unit.conftest import ALPHA_UUID, virtnbdbackup_fake_success


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
        return virtnbdbackup_fake_success(args, check=check, env=env)

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
        return virtnbdbackup_fake_success(args, check=check, env=env)

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
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)
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
            return virtnbdbackup_fake_success(args, check=check, env=env)
        raise CommandError(CommandResult(args, 9, "", "incremental boom"))

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-a")
    (chain_dir / "fixture").write_bytes(b"keep-me")
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-b")
    assert chain_dir.is_dir()
    assert (chain_dir / "fixture").exists()
    assert (chain_dir / CHAIN_POISON_NAME).is_file()
    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-c")
    chain_c = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-c"
    assert chain_c.is_dir()
    captured = capsys.readouterr()
    err = captured.err
    assert "backup failed" in err
    assert "removed partial backup" not in err
    assert "current chain is poisoned; starting new chain" in captured.out
    assert seen == ["full", "inc", "full"]


def test_backup_vm_record_run_failure_reports_dangling_checkpoints(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)
    monkeypatch.setattr("libvirt_backup_system.backup.record_run", lambda *args, **kwargs: False)
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    assert "dangling checkpoints" in capsys.readouterr().err
    month_dir = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05"
    assert not (month_dir / CHAIN_STATE_NAME).exists()
    assert not (month_dir / "stamp").exists()


def test_backup_vm_record_run_failure_with_unreadable_checkpoints(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", virtnbdbackup_fake_success)
    monkeypatch.setattr("libvirt_backup_system.backup.record_run", lambda *args, **kwargs: False)
    from libvirt_backup_system.run_records import list_checkpoints as real_list

    calls = {"n": 0}

    def fail_on_second(chain_dir, vm_name=None):
        calls["n"] += 1
        if calls["n"] > 1:
            raise CheckpointReadError("permission denied")
        return real_list(chain_dir, vm_name)

    monkeypatch.setattr("libvirt_backup_system.backup.list_checkpoints", fail_on_second)
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp")
    err = capsys.readouterr().err
    assert "run record write failed" in err
    assert "dangling checkpoints" not in err
    assert not (tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp").exists()


def test_incremental_record_run_failure_poisons_chain_and_next_run_starts_full(
    tmp_path: Path,
    monkeypatch,
    capsys,
    backup_config,
) -> None:
    cfg = _backup_config(backup_config)
    calls: list[list[str]] = []
    record_calls = {"n": 0}

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        calls.append(args)
        return virtnbdbackup_fake_success(args, check=check, env=env)

    def fake_record(*args, **kwargs) -> bool:
        record_calls["n"] += 1
        return record_calls["n"] != 2

    monkeypatch.setattr("libvirt_backup_system.backup.run_streamed", fake_run)
    monkeypatch.setattr("libvirt_backup_system.backup.record_run", fake_record)

    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-a")
    assert not backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-b")
    chain_a = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-a"
    assert (chain_a / CHAIN_POISON_NAME).is_file()

    assert backup_vm(cfg, VM("alpha", "running", ALPHA_UUID), "2026-05", "stamp-c")
    chain_c = tmp_path / f"backups/host/{ALPHA_UUID}/2026-05/stamp-c"
    assert calls[-1][calls[-1].index("-l") + 1] == "full"
    assert calls[-1][calls[-1].index("-o") + 1] == str(chain_c)
    assert "current chain is poisoned; starting new chain" in capsys.readouterr().out
