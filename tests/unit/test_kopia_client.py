from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_client
from libvirt_backup_system.shell import CommandError, CommandResult

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _write_password(path: Path, value: str = "swordfish") -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def _make_run_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> list[tuple[list[str], Mapping[str, str] | None]]:
    captured: list[tuple[list[str], Mapping[str, str] | None]] = []

    def fake_run(
        args: list[str], *, check: bool = True, env: Mapping[str, str] | None = None, **_: Any
    ) -> CommandResult:
        captured.append((args, env))
        if returncode != 0 and check:
            raise CommandError(CommandResult(args, returncode, stdout, stderr))
        return CommandResult(args, returncode, stdout, stderr)

    monkeypatch.setattr(kopia_client, "run", fake_run)
    monkeypatch.setattr(kopia_client, "run_streamed", fake_run)
    return captured


def test_password_propagates_to_env_and_phone_home_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw", "hunter2\n")
    monkeypatch.setenv("KOPIA_PASSWORD", "stale-value-should-be-overwritten")
    captured = _make_run_capture(monkeypatch, stdout="{}")
    kopia_client.repository_status(config_file=tmp_path / "c", password_file=password, cache_dir=tmp_path / "cache")
    _, env = captured[0]
    assert env is not None
    assert env["KOPIA_PASSWORD"] == "hunter2"
    assert env["KOPIA_CACHE_DIRECTORY"].endswith("cache")
    assert env["KOPIA_CHECK_FOR_UPDATES"] == "false"


def test_password_unreadable_raises_command_error(tmp_path: Path) -> None:
    with pytest.raises(CommandError) as info:
        kopia_client.repository_status(config_file=tmp_path / "c", password_file=tmp_path / "missing")
    assert "kopia password unreadable" in info.value.result.stderr


def test_password_insecure_mode_raises_command_error(tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    password.chmod(0o644)
    with pytest.raises(CommandError) as info:
        kopia_client.repository_status(config_file=tmp_path / "c", password_file=password)
    assert "must be mode 600" in info.value.result.stderr


def test_repository_create_filesystem_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    cfg = tmp_path / "host.config"
    repo = tmp_path / "repo"
    kopia_client.repository_create_filesystem(
        config_file=cfg, repo_path=repo, password_file=password, cache_dir=tmp_path / "cache"
    )
    assert repo.is_dir()
    args, _env = captured[0]
    assert args == [
        "kopia",
        "--config-file",
        str(cfg),
        "repository",
        "create",
        "filesystem",
        "--path",
        str(repo),
    ]


def test_repository_create_filesystem_passes_object_splitter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.repository_create_filesystem(
        config_file=tmp_path / "host.config",
        repo_path=tmp_path / "repo",
        password_file=password,
        object_splitter="FIXED-4M",
    )
    args, _env = captured[0]
    assert "--object-splitter" in args
    assert "FIXED-4M" in args


def test_repository_connect_filesystem_readonly_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    cfg = tmp_path / "peer.config"
    repo = tmp_path / "peer-repo"
    kopia_client.repository_connect_filesystem(config_file=cfg, repo_path=repo, password_file=password, read_only=True)
    args, _ = captured[0]
    assert args[-1] == "--readonly"


def test_repository_status_parses_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    body = (FIXTURE_DIR / "kopia_repository_status.json").read_text(encoding="utf-8")
    _make_run_capture(monkeypatch, stdout=body)
    status = kopia_client.repository_status(config_file=tmp_path / "c", password_file=password)
    assert status["splitter"] == "FIXED-4M"
    assert status["encryption"] == "AES256-GCM-HMAC-SHA256"


def test_repository_status_rejects_non_object_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    _make_run_capture(monkeypatch, stdout="[1, 2, 3]")
    with pytest.raises(ValueError, match="non-object"):
        kopia_client.repository_status(config_file=tmp_path / "c", password_file=password)


def test_policy_set_global_emits_only_supplied_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.policy_set_global(
        config_file=tmp_path / "c",
        password_file=password,
        keep_latest=8,
        keep_daily=30,
        compression="zstd-fastest",
    )
    args, _ = captured[0]
    assert "--keep-latest" in args and "8" in args
    assert "--keep-daily" in args and "30" in args
    assert "--compression" in args and "zstd-fastest" in args
    assert "--keep-hourly" not in args


def test_policy_set_global_noop_when_no_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.policy_set_global(config_file=tmp_path / "c", password_file=password)
    assert captured == []


def test_policy_show_global_parses_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    body = (FIXTURE_DIR / "kopia_policy_show_global.json").read_text(encoding="utf-8")
    _make_run_capture(monkeypatch, stdout=body)
    out = kopia_client.policy_show_global(config_file=tmp_path / "c", password_file=password)
    compression = out["compression"]
    assert isinstance(compression, dict)
    assert compression["compressorName"] == "zstd-fastest"
    retention = out["retention"]
    assert isinstance(retention, dict)
    assert retention["keepLatest"] == 10
    assert retention["keepDaily"] == 14


def test_policy_show_global_rejects_non_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    _make_run_capture(monkeypatch, stdout="[]")
    with pytest.raises(ValueError, match="non-object"):
        kopia_client.policy_show_global(config_file=tmp_path / "c", password_file=password)


def test_maintenance_run_full_safety_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.maintenance_run(config_file=tmp_path / "c", password_file=password, full=True, safety="none")
    args, _ = captured[0]
    assert "--full" in args
    assert "--safety=none" in args


def test_maintenance_run_omits_full_and_safety_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.maintenance_run(config_file=tmp_path / "c", password_file=password)
    args, _ = captured[0]
    assert "--full" not in args
    assert all("--safety=" not in arg for arg in args)
    assert "--dry-run" not in args


def test_maintenance_info_uses_read_only_info_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.maintenance_info(config_file=tmp_path / "c", password_file=password)
    args, _ = captured[0]
    assert args[-2:] == ["maintenance", "info"]


def test_kopia_available_true_and_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def ok_run(args: list[str], **_: Any) -> CommandResult:
        return CommandResult(args, 0, "kopia 0.16.0", "")

    monkeypatch.setattr(kopia_client, "run", ok_run)
    assert kopia_client.kopia_available() is True

    def boom(_args: list[str], **_: Any) -> CommandResult:
        raise OSError("missing")

    monkeypatch.setattr(kopia_client, "run", boom)
    assert kopia_client.kopia_available() is False

    def fail_run(args: list[str], **_: Any) -> CommandResult:
        return CommandResult(args, 1, "", "no")

    monkeypatch.setattr(kopia_client, "run", fail_run)
    assert kopia_client.kopia_available() is False


def test_repository_change_password_uses_new_password_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    captured = _make_run_capture(monkeypatch)
    kopia_client.repository_change_password(config_file=tmp_path / "c", password_file=password, new_password="new-pw")
    args, _env = captured[0]
    assert args[-2:] == ["change-password", "--new-password=new-pw"]


def test_repository_change_password_raises_on_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    password = _write_password(tmp_path / "pw")
    _make_run_capture(monkeypatch, stderr="boom", returncode=17)
    with pytest.raises(CommandError) as info:
        kopia_client.repository_change_password(config_file=tmp_path / "c", password_file=password, new_password="x")
    assert info.value.result.returncode == 17
    assert info.value.result.stderr == "boom"


def test_tags_args_renders_sorted(tmp_path: Path) -> None:
    # ``tags_args`` is consumed by kopia_snapshots; tests live here because the
    # helper itself ships from kopia_client.
    assert kopia_client.tags_args({"b": "1", "a": "2"}) == ["--tags=a:2", "--tags=b:1"]


def test_as_string_helpers_drop_non_string_values() -> None:
    assert kopia_client.as_string_keyed("not-a-dict") == {}
    assert kopia_client.as_string_string({"a": 1, "b": "ok"}) == {"b": "ok"}
