from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

from app.core.time_utils import to_millis


_lock = threading.Lock()
_metrics = {
    "database_lock_errors": 0,
    "last_database_lock_at_ms": None,
    "last_sqlite_error_code": None,
    "last_sqlite_error_name": None,
    "connection_init_errors": 0,
    "connection_init_cancellations": 0,
    "last_connection_init_error_type": None,
    "last_connection_init_error_at_ms": None,
    "journal_mode": None,
    "synchronous": None,
    "synchronous_value": None,
    "startup_verified": False,
    "sqlite_version": None,
    "wal_reset_fixed": False,
    "durability_scope": None,
}


def record_database_lock_error(error=None) -> None:
    with _lock:
        _metrics["database_lock_errors"] += 1
        _metrics["last_database_lock_at_ms"] = to_millis(datetime.now(tz=UTC))
        if error is not None:
            _metrics["last_sqlite_error_code"] = getattr(error, "sqlite_errorcode", None)
            _metrics["last_sqlite_error_name"] = getattr(error, "sqlite_errorname", None)


def record_connection_init_error(error) -> None:
    with _lock:
        _metrics["connection_init_errors"] += 1
        if isinstance(error, asyncio.CancelledError):
            _metrics["connection_init_cancellations"] += 1
        _metrics["last_connection_init_error_type"] = type(error).__name__
        _metrics["last_connection_init_error_at_ms"] = to_millis(datetime.now(tz=UTC))
        _metrics["last_sqlite_error_code"] = getattr(error, "sqlite_errorcode", None)
        _metrics["last_sqlite_error_name"] = getattr(error, "sqlite_errorname", None)


def record_sqlite_startup(configuration: dict | None) -> None:
    with _lock:
        _metrics.update(configuration or {"journal_mode": None, "synchronous": None, "synchronous_value": None,
                                         "sqlite_version": None, "wal_reset_fixed": False, "durability_scope": None})
        _metrics["startup_verified"] = configuration is not None


def sqlite_observability_snapshot() -> dict:
    with _lock:
        return dict(_metrics)
