from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from typing import Any

_LOCK = threading.Lock()


def event(level: str, name: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level.upper(),
        "event": name,
        **fields,
    }
    with _LOCK:
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True), file=sys.stdout, flush=True)
