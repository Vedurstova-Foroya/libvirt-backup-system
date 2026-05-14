from __future__ import annotations

from pathlib import Path

from libvirt_backup_system.config import DEFAULTS, Config
from libvirt_backup_system.installer import install, uninstall
from libvirt_backup_system.shell import CommandResult


def _fake_config_factory(
    tmp_path: Path,
    *,
    backup_path: str | None = None,
) -> object:
    def fake_config(
        config_path: str | None = None,
        prefix: str | None = None,
        *,
        apply_env_overrides: bool = True,
    ) -> Config:
        values = dict(DEFAULTS)
        if backup_path is not None:
            values["BACKUP_PATH"] = backup_path
        return Config(values=values, path=tmp_path / "etc/config.env", prefix=tmp_path)

    return fake_config


def _quoted_systemd_path(path: Path) -> str:
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _escaped_systemd_path(path: Path) -> str:
    # Mirrors libvirt_backup_system.systemd_units._escape_systemd_path: path-
    # typed directives (EnvironmentFile=, RequiresMountsFor=) are emitted
    # unquoted with backslash-escaped whitespace and doubled %.
    escaped = str(path).replace("\\", "\\\\").replace("%", "%%")
    return escaped.replace("\t", "\\\t").replace(" ", "\\ ")


def _patch_prefixed_to_tmp(monkeypatch, tmp_path: Path) -> None:
    fake_prefixed = lambda path, root: tmp_path / str(path).lstrip("/")  # noqa: E731
    monkeypatch.setattr("libvirt_backup_system.installer.prefixed", fake_prefixed)
    # run_systemctl now lives in systemd_units and uses its own ``prefixed``
    # binding to find the unit file; patch that namespace too.
    monkeypatch.setattr("libvirt_backup_system.systemd_units.prefixed", fake_prefixed)


def test_install_and_uninstall_preserves_and_purges(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
    assert install(str(tmp_path)) == 0
    bin_path = tmp_path / "usr/local/bin/libvirt-backup-system"
    config_path = tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    assert bin_path.exists()
    assert config_path.exists()
    assert "BACKUP_PATH=\n" in config_path.read_text(encoding="utf-8")
    assert not service_path.exists()

    (tmp_path / "var/lib/libvirt-backup-system").mkdir(parents=True)
    (tmp_path / "var/log/libvirt-backup-system").mkdir(parents=True)
    assert uninstall(str(tmp_path)) == 0
    assert not bin_path.exists()
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


def test_install_honors_explicit_config_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", Path.exists)
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

    assert install(str(root)) == 0

    config_path = root / "etc/libvirt-backup-system/libvirt-backup.env"
    bin_path = root / "usr/local/bin/libvirt-backup-system"
    service_text = (root / "etc/systemd/system/libvirt-backup-system.service").read_text(encoding="utf-8")
    assert f"EnvironmentFile={_escaped_systemd_path(config_path)}\n" in service_text
    assert (
        f"ExecStart={_quoted_systemd_path(bin_path)} " f"--config {_quoted_systemd_path(config_path)} run"
    ) in service_text
    assert f"RequiresMountsFor={_escaped_systemd_path(backup_dir)}\n" in service_text


def test_install_rejects_relative_config_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    assert install(str(tmp_path), config_path="relative.env") == 1

    assert not (tmp_path / "relative.env").exists()
    err = capsys.readouterr().err
    assert "config_path must be an absolute path for systemd units" in err


def test_install_rejects_control_char_config_path(tmp_path: Path, capsys) -> None:
    assert install(str(tmp_path), config_path=str(tmp_path / "bad\nname.env")) == 1

    err = capsys.readouterr().err
    assert "config_path must not contain control characters for systemd units" in err


def test_install_rejects_relative_backup_path(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("BACKUP_PATH", "relative/backups")

    assert install(str(tmp_path)) == 1

    assert not (tmp_path / "etc/systemd/system/libvirt-backup-system.service").exists()
    err = capsys.readouterr().err
    assert "BACKUP_PATH must be an absolute path for systemd units" in err


def test_install_reports_stale_systemd_unit_removal_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    service_path = tmp_path / "etc/systemd/system/libvirt-backup-system.service"
    service_path.parent.mkdir(parents=True)
    service_path.write_text("stale\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == service_path:
            raise PermissionError("no perms")
        original_unlink(self, *args, **kwargs)

    monkeypatch.setattr("libvirt_backup_system.installer.Path.unlink", fake_unlink)
    assert install(str(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "failed to remove stale systemd unit" in err
    assert "no perms" in err


def test_uninstall_systemd_activation_and_missing_files(tmp_path: Path, monkeypatch) -> None:
    original_exists = Path.exists

    def fake_exists(self: Path) -> bool:
        return True if str(self) == "/run/systemd/system" else original_exists(self)

    monkeypatch.setattr("libvirt_backup_system.installer.root_prefix", lambda prefix=None: Path("/"))
    _patch_prefixed_to_tmp(monkeypatch, tmp_path)
    monkeypatch.setattr("libvirt_backup_system.installer.Config.load", _fake_config_factory(tmp_path))
    monkeypatch.setattr("libvirt_backup_system.installer.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.setattr("libvirt_backup_system.installer.Path.exists", fake_exists)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.run",
        lambda args, check=True, env=None: calls.append(args) or CommandResult(args, 0, "", ""),
    )
    # Both unit files exist, so disable/stop run normally.
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / "libvirt-backup-system.timer").write_text("stale timer\n", encoding="utf-8")
    (systemd_dir / "libvirt-backup-system.service").write_text("stale service\n", encoding="utf-8")
    assert uninstall(None) == 0
    assert calls[0] == ["systemctl", "disable", "--now", "libvirt-backup-system.timer"]
    assert calls[-1] == ["systemctl", "daemon-reload"]
