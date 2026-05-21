"""Helpers + input-validation tests for ``libvirt_backup_system.restore``.

End-to-end overwrite / turnkey orchestration tests live in
``test_restore_paths.py``. Both files share the project's 300-LOC ceiling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from libvirt_backup_system import kopia_snapshots, restore
from libvirt_backup_system.list_restore_points import BackupRow
from libvirt_backup_system.manifest import MANIFEST_FILENAME, ManifestDisk
from libvirt_backup_system.shell import CommandError, CommandResult

from .conftest import ALPHA_UUID
from .restore_helpers import (
    TIMESTAMP,
    ConvertOk,
    ConvertWith,
    KopiaProc,
    Snap,
    make_config,
    make_manifest,
    make_row,
    ok_result,
    run_shutdown,
    run_stream,
)


def test_restore_rejects_invalid_uuid(tmp_path: Path) -> None:
    assert restore.restore(make_config(tmp_path), "not-a-uuid", TIMESTAMP) == 1


def test_restore_rejects_malformed_timestamp(tmp_path: Path) -> None:
    assert restore.restore(make_config(tmp_path), ALPHA_UUID, "..") == 1


def test_restore_fails_when_backup_path_not_mount(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    cfg.values["BACKUP_REQUIRE_NFS_MOUNT"] = "true"
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_no_matching_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [])
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_match_row_skips_non_matching_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_config(tmp_path)
    other = BackupRow(
        vm_uuid=ALPHA_UUID,
        timestamp="20250101T010101",
        host_id="host-a",
        vm_name="",
        run_id="r",
        snapshot_id="s",
        config_file=tmp_path / "kopia.config",
    )
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [other])
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_restore_logs_unsafe_vm_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [make_row(tmp_path)])
    monkeypatch.setattr(restore, "_restore_manifest", lambda *_a, **_k: make_manifest(vm_name="-evil"))
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1
    assert "manifest carries unsafe vm name" in capsys.readouterr().err


@pytest.mark.parametrize("stub_name", ["_ensure_staging_root", "_prepare_staging", "_restore_manifest"])
def test_restore_propagates_helper_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_name: str) -> None:
    cfg = make_config(tmp_path)
    monkeypatch.setattr(restore, "enumerate_backups", lambda _c, *, vm_uuid=None: [make_row(tmp_path)])
    monkeypatch.setattr(restore, stub_name, lambda *_a, **_k: None)
    assert restore.restore(cfg, ALPHA_UUID, TIMESTAMP) == 1


def test_ensure_staging_root_logs_on_mkdir_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = make_config(tmp_path)

    def boom(_self: Path, *_a: Any, **_kw: Any) -> None:
        raise OSError("no space")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert restore._ensure_staging_root(cfg) is None
    assert "restore staging root creation failed" in capsys.readouterr().err


def test_prepare_staging_rejects_unsafe_subpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    monkeypatch.setattr(restore, "subpath_is_safe", lambda _r, _p: False)
    assert restore._prepare_staging(root, ALPHA_UUID, TIMESTAMP) is None
    assert "restore staging path is unsafe" in capsys.readouterr().err


def test_prepare_staging_logs_on_mkdir_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    real_mkdir = Path.mkdir

    def boom(self: Path, *args: Any, **kwargs: Any) -> None:
        if self.name.startswith(ALPHA_UUID):
            raise OSError("no space")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", boom)
    assert restore._prepare_staging(root, ALPHA_UUID, TIMESTAMP) is None
    assert "restore staging dir creation failed" in capsys.readouterr().err


def test_prepare_staging_removes_existing_dir(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    leftover = root / f"{ALPHA_UUID}-{TIMESTAMP}"
    leftover.mkdir()
    (leftover / "stale.txt").write_text("old", encoding="utf-8")
    assert restore._prepare_staging(root, ALPHA_UUID, TIMESTAMP) == leftover
    assert not (leftover / "stale.txt").exists()


def test_restore_manifest_logs_when_kopia_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(**_: Any) -> None:
        raise CommandError(CommandResult(["kopia"], 4, "", "no such snap"))

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", boom)
    staging = tmp_path / "stage"
    staging.mkdir()
    assert restore._restore_manifest(make_config(tmp_path), make_row(tmp_path), staging) is None
    assert "meta snapshot restore failed" in capsys.readouterr().err


def test_restore_manifest_logs_when_parse_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    staging = tmp_path / "stage"
    staging.mkdir()

    def write_garbage(**kwargs: Any) -> None:
        (kwargs["dest"] / MANIFEST_FILENAME).write_text("not valid json", encoding="utf-8")

    monkeypatch.setattr(kopia_snapshots, "snapshot_restore_to_path", write_garbage)
    assert restore._restore_manifest(make_config(tmp_path), make_row(tmp_path), staging) is None
    assert "manifest read failed" in capsys.readouterr().err


def test_disk_snapshot_id_returns_first_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_list(**kwargs: Any) -> list[Any]:
        captured["tags"] = kwargs["tags"]
        return [Snap("snap-xyz")]

    monkeypatch.setattr(kopia_snapshots, "snapshot_list", fake_list)
    assert restore._disk_snapshot_id(make_config(tmp_path), make_row(tmp_path), "vda") == "snap-xyz"
    assert captured["tags"] == {"kind": "disk", "run-id": "run-1", "disk": "vda"}


@pytest.mark.parametrize(
    "raises",
    [CommandError(CommandResult(["kopia"], 3, "", "fail")), ValueError("bad json")],
    ids=["command-error", "value-error"],
)
def test_disk_snapshot_id_swallows_lookup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: Exception
) -> None:
    def boom(**_: Any) -> None:
        raise raises

    monkeypatch.setattr(kopia_snapshots, "snapshot_list", boom)
    assert restore._disk_snapshot_id(make_config(tmp_path), make_row(tmp_path), "vda") is None


def test_disk_snapshot_id_returns_none_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(kopia_snapshots, "snapshot_list", lambda **_: [])
    assert restore._disk_snapshot_id(make_config(tmp_path), make_row(tmp_path), "vda") is None
    assert "disk snapshot missing for run" in capsys.readouterr().err


def test_stream_disk_to_qcow2_qemu_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kopia_proc = KopiaProc()
    assert run_stream(restore, tmp_path, monkeypatch, kopia=kopia_proc, popen=ConvertWith(5, b"boom")) is False
    assert "qemu-img convert failed" in capsys.readouterr().err
    assert kopia_proc.waited is True


def test_stream_disk_to_qcow2_kopia_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_stream(restore, tmp_path, monkeypatch, kopia=KopiaProc(returncode=7), popen=ConvertOk) is False
    assert "kopia restore stream failed" in capsys.readouterr().err


def test_stream_disk_to_qcow2_success_closes_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kopia_proc = KopiaProc()
    assert run_stream(restore, tmp_path, monkeypatch, kopia=kopia_proc, popen=ConvertOk) is True
    assert kopia_proc.stdout.closed is True


def test_stream_disk_to_qcow2_handles_missing_pipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``kopia_proc.stdout is None`` exercises the close-skip branch."""
    assert run_stream(restore, tmp_path, monkeypatch, kopia=KopiaProc(with_stdout=False), popen=ConvertOk) is True


@pytest.mark.parametrize(
    "raises,blank",
    [
        (CommandError(CommandResult(["v"], 1, "", "no domain")), False),
        (OSError("virsh missing"), False),
        (None, True),
    ],
    ids=["command-error", "os-error", "blank-output"],
)
def test_local_domain_name_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: BaseException | None, blank: bool
) -> None:
    def fake_run(args: list[str], **_: Any) -> CommandResult:
        if raises is not None:
            raise raises
        return ok_result(args, "  \n") if blank else ok_result(args)

    monkeypatch.setattr(restore, "run", fake_run)
    assert restore._local_domain_name_for_uuid(make_config(tmp_path), ALPHA_UUID) is None


def test_existing_disk_dir_branches() -> None:
    assert restore._existing_disk_dir(make_manifest()) == Path("/var/lib/libvirt/images")
    two = (
        ManifestDisk(target="vda", source_path="/a/v.qcow2", virtual_size_bytes=1, snapshot_filename="vda.raw"),
        ManifestDisk(target="vdb", source_path="/b/v.qcow2", virtual_size_bytes=1, snapshot_filename="vdb.raw"),
    )
    assert restore._existing_disk_dir(make_manifest(disks=two)) is None


def test_shutdown_destroy_command_error_continues(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``destroy`` returning a CommandError just logs and falls through."""
    assert (
        run_shutdown(
            restore,
            monkeypatch,
            tmp_path,
            side={
                "destroy": CommandError(CommandResult(["virsh"], 1, "", "already off")),
                "domstate": lambda args: ok_result(args, "shut off\n"),
            },
        )
        is True
    )


_SHUT_OFF = lambda args: ok_result(args, "shut off\n")  # noqa: E731


@pytest.mark.parametrize(
    "side,message",
    [
        ({"destroy": OSError("no virsh")}, "virsh destroy unavailable"),
        ({"domstate": CommandError(CommandResult(["v"], 2, "", "lost"))}, "domstate check failed"),
        ({"domstate": lambda args: ok_result(args, "running\n")}, "VM is not shut off"),
        (
            {"domstate": _SHUT_OFF, "undefine": CommandError(CommandResult(["v"], 3, "", "boom"))},
            "undefine failed",
        ),
        ({"domstate": _SHUT_OFF, "undefine": OSError("no virsh")}, "virsh undefine unavailable"),
    ],
    ids=["destroy-os", "domstate-cmd", "still-running", "undefine-cmd", "undefine-os"],
)
def test_shutdown_failure_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    side: dict[str, Any],
    message: str,
) -> None:
    assert run_shutdown(restore, monkeypatch, tmp_path, side=side) is False
    assert message in capsys.readouterr().err
