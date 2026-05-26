"""Shared fixtures and stubs for the preflight test suite."""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import preflight
from libvirt_backup_system.config import Config
from libvirt_backup_system.vms import VM


def make_config(tmp_path: Path, *, host_id: str = "host-a") -> Config:
    backup = tmp_path / "backups"
    backup.mkdir(parents=True, exist_ok=True)
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values.update(
        {
            "BACKUP_PATH": str(backup),
            "BACKUP_REQUIRE_NFS_MOUNT": "false",
            "HOST_ID": host_id,
            "REQUIRE_ROOT": "false",
            "LIBVIRT_URI": "qemu:///system",
        }
    )
    return cfg


def write_password_file(cfg: Config) -> Path:
    path = preflight.prefixed(cfg.get("KOPIA_PASSWORD_FILE"), cfg.prefix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("swordfish\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def stub_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    vms: list[VM] | None = None,
    vms_exc: BaseException | None = None,
    missing_binaries: tuple[str, ...] = (),
    df_kb: int = 10**9,
    estimate_kb: int = 0,
) -> None:
    """Stub the external surface (binaries, libvirt, df, estimate) for collect_check_failures."""

    def fake_which(name: str) -> str | None:
        if name in missing_binaries:
            return None
        return f"/usr/bin/{name}"

    def fake_list_vms(_config: Config) -> list[VM]:
        if vms_exc is not None:
            raise vms_exc
        return vms if vms is not None else [VM("alpha", "running", "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")]

    monkeypatch.setattr(preflight.shutil, "which", fake_which)
    monkeypatch.setattr(preflight, "list_vms", fake_list_vms)
    monkeypatch.setattr(preflight, "_df_available_kb", lambda _path: df_kb)
    monkeypatch.setattr(preflight, "_estimate_required_kb", lambda _cfg, _vms: estimate_kb)
    monkeypatch.setattr(preflight.kopia_repo, "ensure_local_repo", lambda *_a, **_k: 0)
