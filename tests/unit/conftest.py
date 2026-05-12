from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult


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


def virtnbdbackup_fake_success(args: list[str], *, check: bool = True, env: object = None) -> CommandResult:
    """Mock virtnbdbackup that also produces the output directory.

    Production-side, backup_vm() now refuses to mark a backup successful unless
    the destination directory exists when virtnbdbackup returns 0 (a defense
    against hollow successes). Bare ``lambda: CommandResult(...)`` mocks no
    longer model real virtnbdbackup behavior; tests that want the success path
    should route through this helper.
    """
    if args and args[0] == "virtnbdbackup" and "-o" in args:
        Path(args[args.index("-o") + 1]).mkdir(parents=True, exist_ok=True)
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
def _stub_domain_xml_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inactive backups call domain_xml_fingerprint, which would otherwise shell
    # out to virsh and explode in unit tests. Default to a stable stub so each
    # test only needs to override when it wants to assert the fingerprint code
    # path directly.
    monkeypatch.setattr("libvirt_backup_system.backup.domain_xml_fingerprint", lambda uri, name: "fp-stub")
