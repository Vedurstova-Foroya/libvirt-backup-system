from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.config import (
    Config,
    bool_value,
    default_config_path,
    float_value,
    int_value,
    parse_env_file,
    prefixed,
    root_prefix,
    split_words,
)


def test_parse_env_file_and_config_load(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text(
        """
# comment
BACKUP_PATH="/tmp/backups"
VM_BLACKLIST=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
BACKUP_REQUIRE_NFS_MOUNT=false
BROKEN
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_ID", "env-host")
    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path))

    assert parse_env_file(env_file)["BACKUP_PATH"] == "/tmp/backups"
    assert cfg.path == env_file
    assert cfg.prefix == tmp_path
    assert cfg.get("HOST_ID") == "env-host"
    assert cfg.path_value("BACKUP_PATH") == Path("/tmp/backups")
    assert cfg.blacklist == {
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    }
    rendered = cfg.render_env()
    # BACKUP_PATH is rendered uncommented (operator must set it).
    assert "BACKUP_PATH=/tmp/backups\n" in rendered
    # New kopia keys are rendered as commented defaults so operators see them.
    assert "# KOPIA_REPO_PATH=" in rendered
    assert "# KOPIA_PASSWORD_FILE=/etc/libvirt-backup-system/kopia.pw" in rendered
    assert "# KEEP_DAILY=30" in rendered
    assert "# KOPIA_MAINTENANCE_INTERVAL=24h" in rendered
    # Legacy chain-era keys must not appear anywhere in the rendered env.
    assert "BACKUP_COMPRESS" not in rendered
    assert "BACKUP_RETENTION_MONTHS" not in rendered
    assert "BACKUP_CLEANUP_ON_RUN" not in rendered
    assert "BACKUP_INCREMENTAL_MULTIPLIER" not in rendered


def test_config_helpers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIBVIRT_BACKUP_ROOT_PREFIX", str(tmp_path / "root"))
    assert root_prefix() == (tmp_path / "root").resolve()
    assert prefixed("/etc/example", tmp_path) == tmp_path / "etc/example"
    assert prefixed("relative", tmp_path) == tmp_path / "relative"
    assert default_config_path(tmp_path) == tmp_path / "etc/libvirt-backup-system/libvirt-backup.env"
    assert bool_value("YES")
    assert not bool_value("off")
    assert int_value({"x": "3"}, "x") == 3
    assert float_value({"x": "1.5"}, "x") == 1.5
    assert split_words("one,two three") == ["one", "two", "three"]


def test_load_uses_default_config_environment(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "custom.env"
    config_file.write_text("BACKUP_PATH=/mnt/qnap\nBACKUP_REQUIRE_NFS_MOUNT=true\n", encoding="utf-8")
    monkeypatch.setenv("LIBVIRT_BACKUP_CONFIG", str(config_file))
    monkeypatch.delenv("BACKUP_PATH", raising=False)
    cfg = Config.load(prefix=str(tmp_path))
    assert cfg.path == config_file
    assert cfg.path_value("BACKUP_PATH") == Path("/mnt/qnap")
    assert cfg.enabled("BACKUP_REQUIRE_NFS_MOUNT")


def test_parse_missing_env_file(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "missing.env") == {}
    assert "PATH" in os.environ


def test_parse_env_file_handles_malformed_quotes(tmp_path: Path) -> None:
    env_file = tmp_path / "broken.env"
    env_file.write_text('BAD="a"b"\nGOOD="ok"\n', encoding="utf-8")
    values = parse_env_file(env_file)
    assert values["GOOD"] == "ok"
    assert values["BAD"] == 'a"b'


def test_load_ignores_env_overrides_when_disabled(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text("BACKUP_PATH=/from/file\n", encoding="utf-8")
    monkeypatch.setenv("BACKUP_PATH", "/from/env")
    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path), apply_env_overrides=False)
    assert cfg.get("BACKUP_PATH") == "/from/file"


def test_kopia_and_retention_env_vars_override_file_and_defaults(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text(
        """\
KOPIA_REPO_PATH=/from/file/repo
KOPIA_PASSWORD_FILE=/from/file/password
KOPIA_CACHE_DIR=/from/file/cache
KOPIA_PARALLELISM=2
KOPIA_SPLITTER=DYNAMIC-4M-BUZHASH
KOPIA_COMPRESSION=s2-default
KEEP_LATEST=1
KEEP_HOURLY=2
KEEP_DAILY=3
KEEP_WEEKLY=4
KEEP_MONTHLY=5
KEEP_ANNUAL=6
KOPIA_MAINTENANCE_INTERVAL=12h
KOPIA_VERIFY_INTERVAL=3d
""",
        encoding="utf-8",
    )
    overrides = {
        "KOPIA_REPO_PATH": "/from/env/repo",
        "KOPIA_PASSWORD_FILE": "/from/env/password",
        "KOPIA_CACHE_DIR": "/from/env/cache",
        "KOPIA_PARALLELISM": "9",
        "KOPIA_SPLITTER": "FIXED-8M",
        "KOPIA_COMPRESSION": "zstd",
        "KEEP_LATEST": "10",
        "KEEP_HOURLY": "11",
        "KEEP_DAILY": "12",
        "KEEP_WEEKLY": "13",
        "KEEP_MONTHLY": "14",
        "KEEP_ANNUAL": "15",
        "KOPIA_MAINTENANCE_INTERVAL": "6h",
        "KOPIA_VERIFY_INTERVAL": "2d",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)

    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path))

    for key, value in overrides.items():
        assert cfg.get(key) == value


def test_load_leaves_host_id_empty_when_machine_id_missing(tmp_path: Path, monkeypatch) -> None:
    # Config.load returns HOST_ID="" instead of raising so preflight's
    # required-present check surfaces a clean "HOST_ID must not be empty".
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text("BACKUP_PATH=/tmp/backups\nHOST_ID=\n", encoding="utf-8")
    monkeypatch.delenv("HOST_ID", raising=False)

    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path))
    assert cfg.get("HOST_ID") == ""


def test_load_uses_machine_id_as_host_id_fallback(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text("BACKUP_PATH=/tmp/backups\nHOST_ID=\n", encoding="utf-8")
    monkeypatch.delenv("HOST_ID", raising=False)
    machine_id_dir = tmp_path / "etc"
    machine_id_dir.mkdir()
    (machine_id_dir / "machine-id").write_text("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4\n", encoding="utf-8")

    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path))
    assert cfg.get("HOST_ID") == "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


def test_env_override_logs_only_when_value_differs(tmp_path: Path, monkeypatch, capsys) -> None:
    env_file = tmp_path / "libvirt-backup.env"
    env_file.write_text("HOST_ID=match-host\nBACKUP_PATH=/file/path\n", encoding="utf-8")
    monkeypatch.setenv("HOST_ID", "match-host")
    monkeypatch.setenv("BACKUP_PATH", "/env/path")
    Config.load(config_path=str(env_file), prefix=str(tmp_path))
    out_lines = capsys.readouterr().out.splitlines()
    override_messages = [line for line in out_lines if '"env override"' in line and '"BACKUP_PATH"' in line]
    matching_messages = [line for line in out_lines if '"env override"' in line and '"HOST_ID"' in line]
    assert override_messages, "expected env override event for differing BACKUP_PATH"
    assert not matching_messages, "did not expect env override event when value matches file"
