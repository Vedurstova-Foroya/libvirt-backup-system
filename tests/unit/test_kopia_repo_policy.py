from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandResult


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path))
    cfg.values.update({"BACKUP_PATH": str(tmp_path / "backup"), "HOST_ID": "host-a"})
    (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
    return cfg


def _write_password(config: Config) -> None:
    path = kopia_repo.password_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("swordfish\n", encoding="utf-8")
    path.chmod(0o600)


def test_ensure_local_repo_rejects_invalid_retention_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg)
    repo = kopia_repo.local_repo_path(cfg)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "kopia.repository.f").write_text("ok", encoding="utf-8")
    cfg.values["KEEP_DAILY"] = "abc"

    def fake_run(args: list[str], **_: Any) -> CommandResult:
        return CommandResult(args, 0, "", "")

    monkeypatch.setattr(kopia_client, "run", fake_run)
    monkeypatch.setattr(kopia_client, "run_streamed", fake_run)
    assert kopia_repo.ensure_local_repo(cfg) == 1
    assert "kopia policy value invalid" in capsys.readouterr().err
