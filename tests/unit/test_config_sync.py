from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import config_sync
from libvirt_backup_system.cli import main
from libvirt_backup_system.config import Config
from libvirt_backup_system.installer import install
from libvirt_backup_system.systemd_start import start
from tests.unit.conftest import stub_ensure_kopia_repo, write_kopia_password_file


def _config(tmp_path: Path, backup_dir: Path | None = None) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    if backup_dir is not None:
        cfg.values["BACKUP_PATH"] = str(backup_dir)
    return cfg


# --------------------------------------------------------------------------
# Unit-level config_sync helpers
# --------------------------------------------------------------------------


def test_shared_config_path_requires_backup_path(tmp_path: Path) -> None:
    assert config_sync.shared_config_path(_config(tmp_path)) is None
    backup_dir = tmp_path / "backups"
    cfg = _config(tmp_path, backup_dir)
    assert config_sync.shared_config_path(cfg) == backup_dir / "libvirt-backup.env"


def test_seed_writes_once_and_blanks_host_id(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    src = tmp_path / "local.env"
    src.write_text(
        f"BACKUP_PATH={backup_dir}\nHOST_ID=node-a\nKOPIA_COMPRESSION=zstd-better\n",
        encoding="utf-8",
    )
    cfg = _config(tmp_path, backup_dir)

    config_sync.seed_shared_config(cfg, src)

    seed = backup_dir / "libvirt-backup.env"
    text = seed.read_text(encoding="utf-8")
    assert "KOPIA_COMPRESSION=zstd-better" in text
    # HOST_ID is host-specific: it must never be carried into the shared seed.
    assert "# HOST_ID=\n" in text
    assert "node-a" not in text

    # A second seed call must not clobber an existing seed (preserves desync).
    seed.write_text("SENTINEL\n", encoding="utf-8")
    config_sync.seed_shared_config(cfg, src)
    assert seed.read_text(encoding="utf-8") == "SENTINEL\n"


def test_seed_is_noop_without_backup_path(tmp_path: Path) -> None:
    src = tmp_path / "local.env"
    src.write_text("BACKUP_PATH=\n", encoding="utf-8")
    # No BACKUP_PATH -> nowhere to publish; must not raise.
    config_sync.seed_shared_config(_config(tmp_path), src)


def test_pull_returns_none_without_seed_and_values_with_seed(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    cfg = _config(tmp_path, backup_dir)
    assert config_sync.pull_shared_config_values(cfg) is None

    (backup_dir / "libvirt-backup.env").write_text(
        "KOPIA_COMPRESSION=zstd-better\n# HOST_ID=\n",
        encoding="utf-8",
    )
    # The commented HOST_ID line is ignored, so a joiner never inherits it.
    assert config_sync.pull_shared_config_values(cfg) == {"KOPIA_COMPRESSION": "zstd-better"}


def test_update_shared_config_overwrites_existing_seed(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(f"BACKUP_PATH={backup_dir}\nKOPIA_COMPRESSION=zstd-better\n", encoding="utf-8")
    cfg = Config.load(config_path=str(config_path), prefix=str(tmp_path), apply_env_overrides=False)
    seed = backup_dir / "libvirt-backup.env"
    seed.write_text("OLD-SEED\n", encoding="utf-8")

    assert config_sync.update_shared_config(cfg) == 0

    text = seed.read_text(encoding="utf-8")
    assert "OLD-SEED" not in text
    assert "KOPIA_COMPRESSION=zstd-better" in text


def test_update_shared_config_errors_without_backup_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("BACKUP_PATH=\n", encoding="utf-8")
    cfg = Config.load(config_path=str(config_path), prefix=str(tmp_path), apply_env_overrides=False)

    assert config_sync.update_shared_config(cfg) == 1
    assert "BACKUP_PATH is not configured" in capsys.readouterr().err


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def test_cli_update_config_publishes_local_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f"BACKUP_PATH={backup_dir}\nHOST_ID=node-a\nSYSTEMD_ON_CALENDAR=*-*-* 05:05:00\n",
        encoding="utf-8",
    )

    assert main(["--prefix", str(tmp_path), "update-config"]) == 0

    text = (backup_dir / "libvirt-backup.env").read_text(encoding="utf-8")
    assert "SYSTEMD_ON_CALENDAR=*-*-* 05:05:00" in text
    assert "# HOST_ID=\n" in text
    assert "node-a" not in text


# --------------------------------------------------------------------------
# install integration: first node publishes, joining node pulls
# --------------------------------------------------------------------------


def test_install_first_node_publishes_shared_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))

    assert install(str(tmp_path)) == 0

    seed = backup_dir / "libvirt-backup.env"
    assert seed.exists()
    text = seed.read_text(encoding="utf-8")
    assert f"BACKUP_PATH={backup_dir}" in text
    assert "# HOST_ID=\n" in text


def test_install_join_pulls_shared_config_and_install_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    seed = backup_dir / "libvirt-backup.env"
    # The seed records the first node's path; the joining host uses its own
    # install-time BACKUP_PATH, which must win over the seed's recorded value.
    seed.write_text(
        "BACKUP_PATH=/first/node/path\n# HOST_ID=\nSYSTEMD_ON_CALENDAR=*-*-* 05:05:00\nKOPIA_COMPRESSION=zstd-better\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))

    assert install(str(tmp_path)) == 0

    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    text = config_path.read_text(encoding="utf-8")
    assert "SYSTEMD_ON_CALENDAR=*-*-* 05:05:00" in text
    assert "KOPIA_COMPRESSION=zstd-better" in text
    assert f"BACKUP_PATH={backup_dir}" in text
    assert "/first/node/path" not in text
    # The joining host must not clobber the shared seed.
    assert "BACKUP_PATH=/first/node/path" in seed.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# start seeding
# --------------------------------------------------------------------------


def _config_text(backup_dir: Path, *extra: str) -> str:
    return f"BACKUP_PATH={backup_dir}\nBACKUP_REQUIRE_NFS_MOUNT=false\nHOST_ID=host-a\n" + "".join(extra)


def _stub_start_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.systemd_start.systemctl_available", lambda root: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.kopia_repo.ensure_local_repo", lambda *a, **k: 0)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.run_systemctl", lambda root, commands: True)
    monkeypatch.setattr("libvirt_backup_system.systemd_start.preflight.repo_creation_failures", lambda cfg: [])
    monkeypatch.setattr("libvirt_backup_system.systemd_start.preflight.peer_repo_access_failures", lambda cfg: [])


def test_start_seeds_shared_config_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start_side_effects(monkeypatch)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir, "KOPIA_COMPRESSION=zstd-better\n"), encoding="utf-8")

    assert start(str(tmp_path)) == 0

    seed = backup_dir / "libvirt-backup.env"
    assert seed.exists()
    text = seed.read_text(encoding="utf-8")
    assert "KOPIA_COMPRESSION=zstd-better" in text
    assert "# HOST_ID=\n" in text
    assert "host-a" not in text


def test_start_does_not_clobber_existing_shared_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start_side_effects(monkeypatch)
    config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    config.parent.mkdir(parents=True)
    config.write_text(_config_text(backup_dir), encoding="utf-8")
    seed = backup_dir / "libvirt-backup.env"
    seed.write_text("SENTINEL\n", encoding="utf-8")

    assert start(str(tmp_path)) == 0

    # A node that joined earlier and runs start later must not overwrite the seed.
    assert seed.read_text(encoding="utf-8") == "SENTINEL\n"
