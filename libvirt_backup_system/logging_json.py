from __future__ import annotations

import datetime as dt
import json
import sys
from typing import Any


def event(level: str, message: str, **fields: Any) -> None:
    record = {
        "ts": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "level": level,
        "message": message,
    }
    record.update(fields)
    stream = sys.stderr if level in {"error", "warning"} else sys.stdout
    print(json.dumps(record, sort_keys=True, separators=(",", ":"), default=str), file=stream, flush=True)
