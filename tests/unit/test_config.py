from __future__ import annotations

import os
from pathlib import Path

from libvirt_backup_system.config import (
    Config,
    bool_value,
    default_config_path,
    float_value,
    int_value,
    iter_month_dirs,
    month_key,
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
VM_BLACKLIST=alpha,beta gamma
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
    assert cfg.blacklist == {"alpha", "beta", "gamma"}
    assert "BACKUP_PATH=/tmp/backups\n" in cfg.render_env()


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
    assert month_key(2026, 5) == "2026-05"


def test_iter_month_dirs(tmp_path: Path) -> None:
    root = tmp_path / "vm"
    assert list(iter_month_dirs(root)) == []
    for name in ["2026-05", "not-a-month", "2026-04", "2026-5"]:
        (root / name).mkdir(parents=True)
    assert [path.name for path in iter_month_dirs(root)] == ["2026-04", "2026-05"]


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
