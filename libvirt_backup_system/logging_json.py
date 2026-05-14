from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any


def event(level: str, message: str, **fields: Any) -> None:
    # Contract: warning/error go to stderr, everything else to stdout. The
    # split lets callers that need a clean stdout (e.g. ``list-vms --json``,
    # which streams a single JSON document on stdout) redirect Config.load's
    # info events into stderr without losing them. See cli.py's
    # ``redirect_stdout(sys.stderr)`` guard around the list-vms path.
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "level": level,
        "message": message,
    }
    record.update(fields)
    stream = sys.stderr if level in {"error", "warning"} else sys.stdout
    print(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str), file=stream, flush=True)
