from __future__ import annotations

import json
import sys
import time
from typing import Any


def event(level: str, message: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "message": message,
    }
    record.update(fields)
    stream = sys.stderr if level in {"error", "warning"} else sys.stdout
    print(json.dumps(record, sort_keys=True, separators=(",", ":")), file=stream, flush=True)
