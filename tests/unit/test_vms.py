from __future__ import annotations

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult
from libvirt_backup_system.vms import VM, is_safe_vm_uuid, list_vms, resolve_vm_uuid
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
            return CommandResult(args, 0, "alpha\n", "")
        if "domuuid" in args:
            return CommandResult(args, 0, "not-a-uuid\n", "")
        return CommandResult(args, 0, "running\n", "")

    monkeypatch.setattr("libvirt_backup_system.vms.run", fake_run)
    with pytest.raises(ValueError, match="invalid UUID"):
        list_vms(cfg)


def test_resolve_vm_uuid_returns_none_for_unsafe_name(monkeypatch) -> None:
    monkeypatch.delenv("LIBVIRT_URI", raising=False)
    cfg = Config.load(prefix="/tmp")
    monkeypatch.setattr("libvirt_backup_system.vms.run", lambda *a, **kw: pytest.fail("must not invoke virsh"))
    assert resolve_vm_uuid(cfg, "../escape") is None
