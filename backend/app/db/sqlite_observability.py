from __future__ import annotations

import threading
from datetime import UTC, datetime

from app.core.time_utils import to_millis


_lock = threading.Lock()
_metrics = {
    "database_lock_errors": 0,
    "last_database_lock_at_ms": None,
}


def record_database_lock_error() -> None:
    with _lock:
        _metrics["database_lock_errors"] += 1
        _metrics["last_database_lock_at_ms"] = to_millis(datetime.now(tz=UTC))


def sqlite_observability_snapshot() -> dict:
    with _lock:
        return dict(_metrics)
