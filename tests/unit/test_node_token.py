from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from libvirt_backup_system.cli import main
from tests.unit.conftest import write_kopia_password_file


def test_cli_show_token_prints_secret_without_config_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_kopia_password_file(tmp_path, value="shared-secret")
    monkeypatch.setenv("SYSTEMD_ON_CALENDAR", "*-*-* 03:30:00")

    assert main(["--prefix", str(tmp_path), "show-token"]) == 0

    captured = capsys.readouterr()
    assert captured.out == "shared-secret\n"
    assert "env override" in captured.err


def test_cli_show_token_reports_unreadable_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--prefix", str(tmp_path), "show-token"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "kopia token unreadable" in captured.err


def test_cli_add_node_prints_pasteable_install_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_path = tmp_path / "backup path"
    backup_path.mkdir()
    write_kopia_password_file(tmp_path, value="token with spaces")
    monkeypatch.setenv("BACKUP_PATH", str(backup_path))

    assert main(["--prefix", str(tmp_path), "add-node"]) == 0

    captured = capsys.readouterr()
    line = captured.out.strip()
    words = shlex.split(line)
    assert words[:2] == ["sudo", "env"]
    assert f"BACKUP_PATH={backup_path}" in words
    assert "KOPIA_PW=token with spaces" in words
    assert words[-5:] == [
        "libvirt_backup_system",
        "install",
        "--kopia-password-env",
        "KOPIA_PW",
        "--acknowledge-password-loss",
    ]
    assert "env override" in captured.err


def test_cli_add_node_requires_configured_backup_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_kopia_password_file(tmp_path, value="shared-secret")

    assert main(["--prefix", str(tmp_path), "add-node"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "BACKUP_PATH is not configured" in captured.err
    assert "shared-secret" not in captured.err


def test_cli_add_node_reports_unreadable_token_after_backup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_path = tmp_path / "backups"
    backup_path.mkdir()
    monkeypatch.setenv("BACKUP_PATH", str(backup_path))

    assert main(["--prefix", str(tmp_path), "add-node"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "kopia token unreadable" in captured.err
