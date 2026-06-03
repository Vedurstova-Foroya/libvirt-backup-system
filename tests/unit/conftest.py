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


def write_kopia_password_file(tmp_path: Path, value: str = "test-pw") -> Path:
    """Pre-create the kopia password file under ``tmp_path`` for install tests.

    ``installer.install`` now refuses to proceed without a kopia password.
    Tests that drive ``install(str(tmp_path))`` directly satisfy the
    "existing file" branch by dropping a mode-600 password file at the
    prefixed default path before invoking ``install``.
    """
    machine_id = tmp_path / "etc/machine-id"
    machine_id.parent.mkdir(parents=True, exist_ok=True)
    if not machine_id.exists():
        machine_id.write_text("11111111111111111111111111111111\n", encoding="utf-8")
    pw_path = tmp_path / "etc/libvirt-backup-system/kopia.pw"
    pw_path.parent.mkdir(parents=True, exist_ok=True)
    pw_path.write_text(f"{value}\n", encoding="utf-8")
    pw_path.chmod(0o600)
    return pw_path


def stub_ensure_kopia_repo(monkeypatch: pytest.MonkeyPatch, *, return_code: int = 0) -> list[object]:
    """Replace ``kopia_repo.ensure_local_repo`` with a no-op for install tests.

    Install tests built against the pre-kopia engine drive ``install`` end-
    to-end with a real ``BACKUP_PATH``; without this stub the call falls
    through to a real ``kopia`` binary invocation that the unit suite does
    not require. Patching ``kopia_repo.ensure_local_repo`` (instead of the
    thin ``installer._ensure_kopia_repo`` wrapper) leaves the wrapper's
    "skip when BACKUP_PATH is empty" branch covered by the same tests.
    Returns a list the test can inspect to see whether the stub was invoked.
    """
    calls: list[object] = []

    def fake_ensure(cfg: object, *, apply_global_policy: bool = True) -> int:
        calls.append(cfg)
        return return_code

    monkeypatch.setattr("libvirt_backup_system.installer.kopia_repo.ensure_local_repo", fake_ensure)
    monkeypatch.setattr("libvirt_backup_system.installer.preflight.repo_creation_failures", lambda _cfg: [])
    return calls


@pytest.fixture(autouse=True)
def _test_password_files_look_root_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests run as an unprivileged user but model root-owned install files."""
    real_lstat = Path.lstat

    class RootOwnedStat:
        def __init__(self, real: object) -> None:
            self._real = real

        def __getattr__(self, name: str) -> object:
            if name == "st_uid":
                return 0
            return getattr(self._real, name)

    def fake_lstat(self: Path) -> object:
        result = real_lstat(self)
        if self.name in {"kopia.pw", "pw"} or self.name.startswith((".kopia.pw.", ".pw.")):
            return RootOwnedStat(result)
        return result

    monkeypatch.setattr(Path, "lstat", fake_lstat)


@pytest.fixture(autouse=True)
def _stub_install_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``installer.install_kopia`` / ``install_nbdcopy`` with no-ops.

    Every ``install(str(tmp_path))`` path now hits the pinned-binary
    bootstrap (``installer_binaries.install_kopia`` + ``install_nbdcopy``),
    which would otherwise reach out to ``github.com`` / ``deb.debian.org``
    in unit tests. The autouse stub keeps the existing test surface intact
    while the dedicated ``test_installer_binaries.py`` suite exercises the
    real implementation through monkeypatched ``urllib.request`` /
    ``shell.run``.
    """

    def fake_install_kopia(prefix: object = None) -> None:
        return None

    def fake_install_nbdcopy(prefix: object = None) -> None:
        return None

    monkeypatch.setattr("libvirt_backup_system.installer.install_kopia", fake_install_kopia)
    monkeypatch.setattr("libvirt_backup_system.installer.install_nbdcopy", fake_install_nbdcopy)


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
