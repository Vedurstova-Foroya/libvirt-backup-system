from __future__ import annotations

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult
from libvirt_backup_system.vms import (
    VM,
    _normalize_vm_name,
    domain_state,
    is_safe_vm_uuid,
    list_vms,
    resolve_vm_uuid,
)
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


def test_vm_running_property() -> None:
    assert VM("alpha", " running ", ALPHA_UUID).running
    assert not VM("beta", "shut off", BETA_UUID).running


def test_vm_inactive_only_for_shut_off() -> None:
    assert VM("beta", " shut off ", BETA_UUID).inactive
    assert not VM("alpha", "running", ALPHA_UUID).inactive
    for transitional in ("paused", "in shutdown", "crashed", "pmsuspended", "blocked"):
        assert not VM("gamma", transitional).inactive, transitional


@pytest.mark.parametrize(
    "uuid",
    [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "00000000-0000-0000-0000-000000000001",
        "deadbeef-1234-5678-9abc-def012345678",
    ],
)
def test_is_safe_vm_uuid_accepts_canonical(uuid: str) -> None:
    assert is_safe_vm_uuid(uuid)


@pytest.mark.parametrize(
    "uuid",
    [
        "",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",  # uppercase
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa",  # short
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # no dashes
        "../escape-uuid-attempt-still-bad-form",
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaag",  # non-hex
    ],
)
def test_is_safe_vm_uuid_rejects_malformed(uuid: str) -> None:
    assert not is_safe_vm_uuid(uuid)


def test_list_vms_rejects_invalid_uuid(monkeypatch) -> None:
    # virsh handing back something that isn't a libvirt-canonical UUID (bad
    # locale, custom build, plugin) must surface as a hard error rather than
    # being written into the on-disk path layout.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if "list" in args:
            return CommandResult(args, 0, "not-a-uuid alpha\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    with pytest.raises(ValueError, match="invalid UUID"):
        list_vms(cfg)


def test_resolve_vm_uuid_returns_none_for_unsafe_name(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    monkeypatch.setattr("libvirt_backup_system.vms.run", lambda *a, **kw: pytest.fail("must not invoke virsh"))
    assert resolve_vm_uuid(cfg, "../escape") is None


def test_resolve_vm_uuid_returns_uuid_on_success(monkeypatch) -> None:
    # Exercises the success path through ``_domuuid`` — separate test so the
    # virsh-failure case below stays focused on the swallow-error semantics.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, ALPHA_UUID + "\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert resolve_vm_uuid(cfg, "alpha") == ALPHA_UUID


def test_resolve_vm_uuid_returns_none_when_domuuid_returns_garbage(monkeypatch, capsys) -> None:
    # ``virsh domuuid`` handing back a malformed UUID must surface as None so
    # the caller logs "not found" instead of writing the bogus UUID into a
    # backup path.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        return CommandResult(args, 0, "not-a-uuid\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert resolve_vm_uuid(cfg, "alpha") is None
    assert "VM name did not resolve to a UUID" in capsys.readouterr().out


def test_resolve_vm_uuid_swallows_virsh_failure(monkeypatch, capsys) -> None:
    # A transient virsh failure or a renamed/missing VM must surface as None
    # so verify --vm <name> can fall back to a clean "not found" message.
    from libvirt_backup_system.shell import CommandError, CommandResult

    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 1, "", "gone"))

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert resolve_vm_uuid(cfg, "alpha") is None
    assert "VM name did not resolve to a UUID" in capsys.readouterr().out


def test_normalize_vm_name_folds_nfc_and_nfd() -> None:
    # ``é`` can be a single codepoint (NFC, U+00E9) or a base ``e`` + combining
    # acute (NFD, U+0065 U+0301). _normalize_vm_name must fold both into the
    # same NFC form so the two representations land in one backup directory.
    nfc = "café"
    nfd = "café"
    assert _normalize_vm_name(nfc) == nfc
    assert _normalize_vm_name(nfd) == nfc


def test_list_vms_blacklist_matches_unicode_normalization(monkeypatch) -> None:
    # An operator who pasted the NFD form of a name into VM_BLACKLIST must
    # still match the NFC-normalized name returned by virsh.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    cfg.values["VM_BLACKLIST"] = "café"

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if "list" in args:
            return CommandResult(args, 0, f"{ALPHA_UUID} café\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert list_vms(cfg) == []


def test_list_vms_skips_malformed_listing_lines(monkeypatch) -> None:
    # virsh occasionally prints a header or footer line that lacks both a UUID
    # and a name; the parser must skip rather than crash. The well-formed row
    # below is the only one that survives and is returned.
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if "list" in args:
            return CommandResult(args, 0, f"singleton\n{ALPHA_UUID} alpha\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert list_vms(cfg) == [VM("alpha", "running", ALPHA_UUID)]


def test_domain_state_returns_stripped_stdout(monkeypatch) -> None:
    cfg = Config.load(prefix="/tmp")

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        assert "domstate" in args
        return CommandResult(args, 0, "shut off\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    assert domain_state(cfg, "alpha") == "shut off"


def test_domain_state_returns_none_for_unsafe_name() -> None:
    # Defense in depth: the caller already rejects unsafe names upstream, but
    # domain_state belongs to the boundary that shells out to virsh and must
    # not pass an unsafe value through.
    cfg = Config.load(prefix="/tmp")
    assert domain_state(cfg, "../escape") is None


def test_domain_state_returns_none_on_command_failure(monkeypatch, capsys) -> None:
    cfg = Config.load(prefix="/tmp")

    def fail_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        raise CommandError(CommandResult(args, 1, "", "no such domain"))

    monkeypatch.setattr("libvirt_backup_system.vms.run", fail_run)
    assert domain_state(cfg, "alpha") is None
    assert "VM state recheck failed" in capsys.readouterr().err
