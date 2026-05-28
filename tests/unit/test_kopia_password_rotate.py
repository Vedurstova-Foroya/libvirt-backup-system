"""Tests for ``kopia_password.change_local_password``.

The rotation path is exercised separately so the read/write contract tests in
``test_kopia_password`` stay focused on resolve/read/write/secure semantics
while these tests pin down the order of operations during an in-place rotation
and the rollback / surfaced-error guarantees when an individual step fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from libvirt_backup_system import kopia_client, kopia_password, kopia_repo
from libvirt_backup_system.config import Config
from libvirt_backup_system.shell import CommandError, CommandResult


def _make_config(tmp_path: Path) -> Config:
    cfg = Config.load(prefix=str(tmp_path), apply_env_overrides=False)
    cfg.values["HOST_ID"] = "host-a"
    cfg.values["BACKUP_PATH"] = str(tmp_path / "backup")
    return cfg


def _write_password(cfg: Config, value: str = "swordfish") -> Path:
    path = kopia_repo.password_file_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _connected_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_file = tmp_path / "connected.config"
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: config_file)
    return config_file


def test_change_local_password_no_op_when_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="hunter2")
    assert kopia_password.change_local_password(cfg, "hunter2") == 0
    assert "kopia password unchanged" in capsys.readouterr().out


def test_change_local_password_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    config_file = _connected_config(tmp_path, monkeypatch)
    status_calls: list[dict[str, object]] = []

    def fake_status(**kwargs: object) -> dict[str, object]:
        status_calls.append(kwargs)
        return {"connected": True}

    monkeypatch.setattr(
        kopia_client,
        "repository_status",
        fake_status,
    )
    change_calls: list[dict[str, object]] = []

    def fake_change(**kwargs: object) -> None:
        change_calls.append(kwargs)

    monkeypatch.setattr(kopia_client, "repository_change_password", fake_change)
    assert kopia_password.change_local_password(cfg, "new-pw") == 0
    assert status_calls and status_calls[0]["config_file"] == config_file
    assert change_calls and change_calls[0]["new_password"] == "new-pw"
    assert change_calls[0]["config_file"] == config_file
    # File now reflects the rotated password.
    assert kopia_password.read_password_file(cfg) == "new-pw"


def test_change_local_password_aborts_when_local_connect_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    monkeypatch.setattr(kopia_repo, "ensure_local_connected", lambda _cfg: None)
    monkeypatch.setattr(
        kopia_client,
        "repository_status",
        lambda **_: pytest.fail("must not run after connect fails"),
    )
    monkeypatch.setattr(
        kopia_client,
        "repository_change_password",
        lambda **_: pytest.fail("must not run after connect fails"),
    )
    assert kopia_password.change_local_password(cfg, "new-pw") == 1
    assert "did not connect with current password" in capsys.readouterr().err
    assert kopia_password.read_password_file(cfg) == "old-pw"


def test_change_local_password_aborts_when_repo_status_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    _connected_config(tmp_path, monkeypatch)

    def fail(**_: object) -> None:
        raise CommandError(CommandResult(["kopia"], 1, "", "no repo"))

    monkeypatch.setattr(kopia_client, "repository_status", fail)
    monkeypatch.setattr(
        kopia_client,
        "repository_change_password",
        lambda **_: pytest.fail("must not run after status fails"),
    )
    assert kopia_password.change_local_password(cfg, "new-pw") == 1
    assert "did not connect with current password" in capsys.readouterr().err
    # On-disk file must still hold the old password.
    assert kopia_password.read_password_file(cfg) == "old-pw"


def test_change_local_password_aborts_when_repo_status_returns_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    _connected_config(tmp_path, monkeypatch)

    def status_returns_garbage(**_: object) -> dict[str, object]:
        raise ValueError("kopia returned a non-object JSON document")

    monkeypatch.setattr(kopia_client, "repository_status", status_returns_garbage)
    assert kopia_password.change_local_password(cfg, "new-pw") == 1
    assert "did not connect with current password" in capsys.readouterr().err


def test_change_local_password_aborts_when_change_password_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    _connected_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kopia_client,
        "repository_status",
        lambda **_: {"connected": True},
    )

    def fail(**_: object) -> None:
        raise CommandError(CommandResult(["kopia"], 1, "", "rotate failed"))

    monkeypatch.setattr(kopia_client, "repository_change_password", fail)
    assert kopia_password.change_local_password(cfg, "new-pw") == 1
    err = capsys.readouterr().err
    assert "kopia change-password failed" in err
    assert "rotate failed" in err
    # Master key wrap rolled back -> file still holds the old password.
    assert kopia_password.read_password_file(cfg) == "old-pw"


def test_change_local_password_reports_write_failure_after_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = _make_config(tmp_path)
    _write_password(cfg, value="old-pw")
    _connected_config(tmp_path, monkeypatch)
    monkeypatch.setattr(
        kopia_client,
        "repository_status",
        lambda **_: {"connected": True},
    )
    monkeypatch.setattr(
        kopia_client,
        "repository_change_password",
        lambda **_: None,
    )

    def fail_write(_cfg: object, _value: str) -> None:
        raise OSError(28, "disk full")

    monkeypatch.setattr(kopia_password, "write_password_file", fail_write)
    assert kopia_password.change_local_password(cfg, "new-pw") == 1
    err = capsys.readouterr().err
    assert "write failed AFTER rotation" in err
    assert "recover manually" in err
    assert "old-pw" in err
    assert "new-pw" in err
