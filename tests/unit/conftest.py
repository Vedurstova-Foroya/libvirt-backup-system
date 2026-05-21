from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system.config import Config

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
