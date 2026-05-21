from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import Config
from libvirt_backup_system.installer import install, uninstall
from tests.unit.conftest import stub_ensure_kopia_repo, write_kopia_password_file


def _quoted_systemd_path(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _escaped_systemd_path(path: Path) -> str:
    # Mirrors libvirt_backup_system.systemd_units._escape_systemd_path: path-
    # typed directives (EnvironmentFile=, RequiresMountsFor=) are emitted
    # unquoted with backslash-escaped whitespace and doubled %.
    escaped = str(path).replace("\\", "\\\\").replace("%", "%%")
    return escaped.replace("\t", "\\\t").replace(" ", "\\ ")


def test_install_and_uninstall_preserves_and_purges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    assert install(str(tmp_path)) == 0
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    assert bin_path.exists()
    assert config_path.exists()
    assert "BACKUP_PATH=\n" in config_path.read_text(encoding="utf-8")
    assert not service_path.exists()

    fish_path = tmp_path / "usr/share/fish/vendor_completions.d/libvirt-backup-system.fish"
    assert fish_path.is_file()
    (tmp_path / "var/lib/libvirt-backup-system").mkdir(parents=True, exist_ok=True)
    (tmp_path / "var/log/libvirt-backup-system").mkdir(parents=True)
    assert uninstall(str(tmp_path)) == 0
    assert not bin_path.exists()
    assert not fish_path.exists()
    assert config_path.exists()

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "BACKUP_PATH=",
            f"BACKUP_PATH={tmp_path / 'backups'}",
        ),
        encoding="utf-8",
    )
    assert install(str(tmp_path)) == 0
    service_text = service_path.read_text(encoding="utf-8")
    check_service_path = tmp_path / "etc/systemd/system/libvirt-backup-system-check.service"
    check_service_text = check_service_path.read_text(encoding="utf-8")
    assert (
        f"ExecStart={_quoted_systemd_path(bin_path)} " f"--config {_quoted_systemd_path(config_path)} run"
    ) in service_text
    assert (
        f"ExecStart={_quoted_systemd_path(bin_path)} " f"--config {_quoted_systemd_path(config_path)} check"
    ) in check_service_text
    assert f"EnvironmentFile={_escaped_systemd_path(config_path)}\n" in service_text
    assert f"EnvironmentFile={_escaped_systemd_path(config_path)}\n" in check_service_text
    assert f"RequiresMountsFor={_escaped_systemd_path(tmp_path / 'backups')}\n" in service_text
    assert f"RequiresMountsFor={_escaped_systemd_path(tmp_path / 'backups')}\n" in check_service_text
    assert "TimeoutStartSec=infinity" in service_text
    assert "KillMode=mixed" in service_text
    assert "TimeoutStopSec=30min" in service_text
    assert "NoNewPrivileges=yes" in service_text
    # StateDirectory= creates /var/lib/libvirt-backup-system at service start
    # so lock.py's run-lock mkdir succeeds on a fresh install.
    assert "StateDirectory=libvirt-backup-system" in service_text
    # Filesystem sandboxing (ProtectSystem, ProtectHome, ReadWritePaths) is
    # intentionally absent: it would hide /home-resident VM disk roots and
    # backup destinations from the service.
    assert "ProtectSystem=" not in service_text
    assert "ProtectHome=" not in service_text
    assert "ReadWritePaths=" not in service_text
    timer_path = tmp_path / "etc/systemd/system/libvirt-backup-system.timer"
    assert "OnCalendar=" in timer_path.read_text(encoding="utf-8")

    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"BACKUP_PATH={tmp_path / 'backups'}",
            "BACKUP_PATH=",
        ),
        encoding="utf-8",
    )
    assert install(str(tmp_path)) == 0
    assert not service_path.exists()
    assert not check_service_path.exists()
    assert not timer_path.exists()

    assert uninstall(str(tmp_path), purge_config=True, purge_state=True, purge_logs=True) == 0
    assert not config_path.exists()


def test_first_install_applies_env_overrides_then_subsequent_install_locks_to_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    env_path = tmp_path / "from-env"
    env_path.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(env_path))
    assert install(str(tmp_path)) == 0
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    assert f"BACKUP_PATH={env_path}" in config_path.read_text(encoding="utf-8")
    assert f"RequiresMountsFor={_escaped_systemd_path(env_path)}\n" in service_path.read_text(encoding="utf-8")

    file_path = tmp_path / "from-file"
    file_path.mkdir()
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            f"BACKUP_PATH={env_path}",
            f"BACKUP_PATH={file_path}",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKUP_PATH", str(tmp_path / "ignored"))
    assert install(str(tmp_path)) == 0
    service_text = service_path.read_text(encoding="utf-8")
    assert f"RequiresMountsFor={_escaped_systemd_path(file_path)}\n" in service_text
    assert "ignored" not in service_text


def test_existing_config_logs_ignored_install_time_env(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("BACKUP_PATH=\n", encoding="utf-8")
    write_kopia_password_file(tmp_path)
    monkeypatch.setenv("BACKUP_PATH", str(tmp_path / "ignored"))

    assert install(str(tmp_path)) == 0

    out = capsys.readouterr().out
    assert "install-time environment ignored" in out
    assert "BACKUP_PATH" in out


def test_install_reports_lock_busy(tmp_path: Path, monkeypatch, capsys) -> None:
    from libvirt_backup_system.lock import acquire_run_lock

    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    with acquire_run_lock(cfg):
        assert install(str(tmp_path)) == 1
    assert "another run in progress" in capsys.readouterr().err


def test_install_honors_explicit_config_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    write_kopia_password_file(tmp_path)
    stub_ensure_kopia_repo(monkeypatch)
    custom_config = tmp_path / "custom/libvirt-backup.env"
    backup_dir = tmp_path / "custom-backups"
    backup_dir.mkdir()
    assert install(str(tmp_path), config_path=str(custom_config)) == 0
    assert custom_config.exists()
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))
    assert install(str(tmp_path), config_path=str(custom_config)) == 0
    default_config = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    assert not default_config.exists()

    custom_config.write_text(
        custom_config.read_text(encoding="utf-8").replace("BACKUP_PATH=", f"BACKUP_PATH={backup_dir}"),
        encoding="utf-8",
    )
    assert install(str(tmp_path), config_path=str(custom_config)) == 0
    service_text = (tmp_path / "etc/systemd/system/libvirt-backup-system.service").read_text(encoding="utf-8")
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    assert f"EnvironmentFile={_escaped_systemd_path(custom_config)}\n" in service_text
    assert (
        f"ExecStart={_quoted_systemd_path(bin_path)} " f"--config {_quoted_systemd_path(custom_config)} run"
    ) in service_text


def test_install_renders_systemd_safe_paths_with_spaces_and_specifiers(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root with spaces 100% $ROOT"
    backup_dir = root / "backup dir 50% $BACKUP"
    backup_dir.mkdir(parents=True)
    monkeypatch.setenv("BACKUP_PATH", str(backup_dir))
    write_kopia_password_file(root)
    stub_ensure_kopia_repo(monkeypatch)

    assert install(str(root)) == 0

    config_path = root / "etc/libvirt-backup-system/libvirt-backup.env"
    bin_path = root / "usr/local/bin/libvirt-backup-system"
    service_text = (root / "etc/systemd/system/libvirt-backup-system.service").read_text(encoding="utf-8")
    assert f"EnvironmentFile={_escaped_systemd_path(config_path)}\n" in service_text
    assert (
        f"ExecStart={_quoted_systemd_path(bin_path)} " f"--config {_quoted_systemd_path(config_path)} run"
    ) in service_text
    assert f"RequiresMountsFor={_escaped_systemd_path(backup_dir)}\n" in service_text
