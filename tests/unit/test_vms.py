from __future__ import annotations

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import (
    VM,
    _normalize_vm_name,
    is_safe_vm_uuid,
    list_vms,
    resolve_vm_uuid,
)
from tests.unit.conftest import ALPHA_UUID, BETA_UUID


def test_vm_running_property() -> None:
    assert VM("alpha", " running ", ALPHA_UUID).running
    for non_running in ("shut off", "paused", "in shutdown", "crashed", "pmsuspended", "blocked"):
        assert not VM("beta", non_running, BETA_UUID).running, non_running


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


def test_list_vms_blacklist_matches_by_uuid(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    cfg.values["VM_BLACKLIST"] = ALPHA_UUID

    def fake_run(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
        if "list" in args:
            return CommandResult(args, 0, f"{ALPHA_UUID} alpha\n{BETA_UUID} beta\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    result = list_vms(cfg)
    assert result == [VM("beta", "running", BETA_UUID)]


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
