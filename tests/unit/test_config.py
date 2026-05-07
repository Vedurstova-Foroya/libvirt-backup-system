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
LOCAL_ROOT="/tmp/backups"
VM_BLACKLIST=alpha,beta gamma
REMOTE_USER=backup
REMOTE_HOST=qnap
SSH_KEY=/key
SSH_OPTIONS="-o BatchMode=yes -o StrictHostKeyChecking=no"
BROKEN
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOST_ID", "env-host")
    cfg = Config.load(config_path=str(env_file), prefix=str(tmp_path))

    assert parse_env_file(env_file)["LOCAL_ROOT"] == "/tmp/backups"
    assert cfg.path == env_file
    assert cfg.prefix == tmp_path
    assert cfg.get("HOST_ID") == "env-host"
    assert cfg.path_value("LOCAL_ROOT") == Path("/tmp/backups")
    assert cfg.blacklist == {"alpha", "beta", "gamma"}
    assert cfg.ssh_base == [
        "ssh",
        "-p",
        "22",
        "-i",
        "/key",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
    ]
    assert cfg.remote_target == "backup@qnap"
    assert "LOCAL_ROOT=/tmp/backups\n" in cfg.render_env()


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
    config_file.write_text("REMOTE_ENABLED=false\nREMOTE_USER=\nREMOTE_HOST=qnap\n", encoding="utf-8")
    monkeypatch.setenv("LIBVIRT_BACKUP_CONFIG", str(config_file))
    monkeypatch.delenv("REMOTE_HOST", raising=False)
    cfg = Config.load(prefix=str(tmp_path))
    assert cfg.path == config_file
    assert cfg.remote_target == "qnap"
    assert not cfg.enabled("REMOTE_ENABLED")


def test_parse_missing_env_file(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "missing.env") == {}
    assert "PATH" in os.environ
