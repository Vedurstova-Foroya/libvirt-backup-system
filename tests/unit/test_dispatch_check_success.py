from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from libvirt_backup_system.systemd_units import CHECK_UNIT_NAME, DISPATCH_OPT_OUT_ENV, dispatch_via_systemd


def test_dispatch_check_prints_passed_on_success(tmp_path: Path, monkeypatch, capsys) -> None:
    systemd_dir = tmp_path / "etc/systemd/system"
    systemd_dir.mkdir(parents=True)
    (systemd_dir / CHECK_UNIT_NAME).write_text("[Unit]\n", encoding="utf-8")
    monkeypatch.setattr("libvirt_backup_system.systemd_units.root_prefix", lambda prefix=None: Path("/"))
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.prefixed", lambda path, root: tmp_path / str(path).lstrip("/")
    )
    monkeypatch.setattr("libvirt_backup_system.systemd_units.shutil.which", lambda binary: "/bin/systemctl")
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.delenv(DISPATCH_OPT_OUT_ENV, raising=False)
    real_exists = Path.exists
    monkeypatch.setattr(
        "libvirt_backup_system.systemd_units.Path.exists",
        lambda self: True if str(self) == "/run/systemd/system" else real_exists(self),
    )

    class _Result:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(args: list[str], **_kwargs: Any) -> _Result:
        if args[:3] == ["systemctl", "start", "--wait"]:
            return _Result(0)
        if args == ["systemctl", "show", CHECK_UNIT_NAME, "--property=InvocationID", "--value"]:
            return _Result(0, "")
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert dispatch_via_systemd("check", prefix=None, config_path=None) == 0
    assert "check passed" in capsys.readouterr().out
