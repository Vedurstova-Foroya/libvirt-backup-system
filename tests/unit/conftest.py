from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult

# Placeholder UUIDs used across the suite. Real ones come from ``virsh
# domuuid``; tests construct VM() objects without going through ``list_vms``
# so they need a syntactically-valid stand-in that ``is_safe_vm_uuid`` accepts.
ALPHA_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BETA_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
GAMMA_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture(autouse=True)
def _isolate_host_config(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the default install prefix to a per-session tmp dir so any
    # ``Config.load()`` call that falls through with ``prefix=None`` resolves
    # ``default_config_path`` under tmp instead of the real
    # ``/etc/libvirt-backup-system/libvirt-backup.env``. On CI that file does
    # not exist, but a developer host that already ran ``install`` owns the
    # file as root:root 0600, which makes ``parse_env_file`` raise
    # ``PermissionError`` instead of returning the empty dict the suite
    # implicitly assumes. Tests that need a specific prefix still pass it
    # explicitly; this fixture only changes the otherwise-undefined default.
    isolated_root = tmp_path_factory.mktemp("isolated_root")
    monkeypatch.setenv("LIBVIRT_BACKUP_ROOT_PREFIX", str(isolated_root))
    etc = isolated_root / "etc"
    etc.mkdir(exist_ok=True)
    (etc / "machine-id").write_text("00000000000000000000000000000000\n", encoding="utf-8")


def virtnbdbackup_fake_success(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
    """Mock virtnbdbackup that also produces the output directory and a new checkpoint.

    Production-side, backup_vm() now refuses to mark a backup successful unless
    the destination directory exists when virtnbdbackup returns 0 (a defense
    against hollow successes), and run_records.record_run treats a missing new
    checkpoint as benign while a write failure aborts the backup. To exercise
    the success path with the same semantics as the e2e fake, append a new
    checkpoint entry to ``<vm>.cpt`` so ``list_checkpoints`` sees a delta and
    ``record_run`` writes a meaningful entry.
    """
    if not args or args[0] != "virtnbdbackup" or "-o" not in args or "-d" not in args:
        return CommandResult(args, 0, "", "")
    dest = Path(args[args.index("-o") + 1])
    vm_name = args[args.index("-d") + 1]
    dest.mkdir(parents=True, exist_ok=True)
    cpt_path = dest / f"{vm_name}.cpt"
    try:
        existing = json.loads(cpt_path.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            existing = []
    except (OSError, json.JSONDecodeError):
        existing = []
    existing.append(f"virtnbdbackup.{vm_name}.{secrets.token_hex(4)}")
    cpt_path.write_text(json.dumps(existing), encoding="utf-8")
    return CommandResult(args, 0, "", "")


@pytest.fixture
def backup_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update(
        {
            "BACKUP_PATH": str(tmp_path / "backups"),
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": "host",
        }
    )
    return cfg


@pytest.fixture(autouse=True)
def _stub_domain_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # _finalize_inactive_marker re-reads virsh domstate after the copy to
    # confirm the VM is still shut off. Default to a stub that matches the
    # original VM state expectation so unit tests don't shell out; tests
    # that exercise the mid-copy state drift override this explicitly.
    monkeypatch.setattr("libvirt_backup_system.backup.domain_state", lambda cfg, name: "shut off")


@pytest.fixture(autouse=True)
def _stub_domain_xml_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inactive backups call domain_xml_fingerprint, which would otherwise shell
    # out to virsh and explode in unit tests. Default to a stable stub so each
    # test only needs to override when it wants to assert the fingerprint code
    # path directly.
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: "fp-stub")


@pytest.fixture(autouse=True)
def _stub_virtnbdbackup_socket_args(monkeypatch: pytest.MonkeyPatch) -> None:
    # backup_vm asks nbd_probe.virtnbdbackup_socket_args whether to pass
    # ``-f``/``--socketfile`` to virtnbdbackup; the lookup shells out to
    # ``virsh domid`` against the configured libvirt URI. Default to ``[]``
    # so existing tests keep asserting the unaugmented virtnbdbackup command;
    # tests that exercise the --socketfile path override this stub.
    monkeypatch.setattr("libvirt_backup_system.backup.virtnbdbackup_socket_args", lambda uri, name: [])
