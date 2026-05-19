from __future__ import annotations

import subprocess
from pathlib import Path

from .config import prefixed
from .logging_json import event


def manual_run_ready(root: Path, *, run_unit_name: str, timer_unit_name: str) -> bool:
    systemd_dir = prefixed("/etc/systemd/system", root)
    run_unit = systemd_dir / run_unit_name
    timer_unit = systemd_dir / timer_unit_name
    if not run_unit.exists() or not timer_unit.exists():
        event(
            "error",
            "backup service is not running; run start before run",
            unit=run_unit_name,
            timer=timer_unit_name,
        )
        return False
    result = subprocess.run(
        [
            "systemctl",
            "show",
            timer_unit_name,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=UnitFileState",
            "--value",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    values = result.stdout.splitlines()
    load_state = values[0] if len(values) > 0 else ""
    active_state = values[1] if len(values) > 1 else ""
    unit_file_state = values[2] if len(values) > 2 else ""
    if result.returncode == 0 and load_state == "loaded" and active_state == "active" and unit_file_state == "enabled":
        return True
    event(
        "error",
        "backup service is not running; run start before run",
        unit=run_unit_name,
        timer=timer_unit_name,
        load_state=load_state or "unknown",
        active_state=active_state or "unknown",
        unit_file_state=unit_file_state or "unknown",
    )
    return False
