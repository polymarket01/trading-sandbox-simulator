"""Verify file-backed SQLite WAL before migrations or application services start.

Journal mode belongs to the database, not to a pooled connection or a schema
migration transaction. This short-lived autocommit connection closes on every
outcome. It never retries an order, changes account data or forces per-order fsync.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from sqlalchemy.engine import make_url
from sqlalchemy.dialects.sqlite.pysqlite import SQLiteDialect_pysqlite

from app.core.config import settings
from app.db.sqlite_observability import record_sqlite_startup
from app.services.persistence_contract import facts_durable


def sqlite_synchronous_mode() -> str:
    # WAL+NORMAL protects database consistency without adding a sync to every
    # commit. It is process-crash recoverable, not a power-loss no-loss promise.
    if facts_durable() or settings.exchange_durability_mode != "sandbox_fast":
        return "NORMAL"
    return "OFF"


def sqlite_wal_reset_fixed(version: tuple[int, ...]) -> bool:
    """Official fixed releases, including the two maintained backports."""
    return (version >= (3, 51, 3) or (version[:2] == (3, 50) and version >= (3, 50, 7))
            or (version[:2] == (3, 44) and version >= (3, 44, 6)))


def initialize_sqlite_wal(
    database_url: str | None,
    *,
    busy_timeout_ms: int = 15_000,
    synchronous: str | None = None,
) -> dict | None:
    record_sqlite_startup(None)
    if not database_url or not database_url.startswith("sqlite"):
        return None
    url = make_url(database_url)
    if not url.database or url.database == ":memory:" or url.query.get("mode") == "memory" or url.database.startswith("file::memory:"):
        return None
    if not sqlite_wal_reset_fixed(sqlite3.sqlite_version_info):
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} has no verified WAL-reset fix; "
            "use SQLite 3.51.3+ (or official 3.44.6/3.50.7 backport) before starting this database"
        )
    synchronous = (synchronous or sqlite_synchronous_mode()).upper()
    if synchronous not in {"OFF", "NORMAL"}:
        raise ValueError("unsupported SQLite durability contract")
    # Reuse SQLAlchemy's URL/URI parsing, including encoded paths and mode=ro.
    args, kwargs = SQLiteDialect_pysqlite().create_connect_args(url)
    kwargs.update(timeout=max(busy_timeout_ms, 100) / 1000, isolation_level=None)
    with closing(sqlite3.connect(*args, **kwargs)) as connection:
        mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if mode != "wal":
            mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        if mode != "wal":
            raise RuntimeError(f"SQLite requires WAL before startup; actual journal_mode={mode}")
        connection.execute(f"PRAGMA synchronous={synchronous}")
        value = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        if value != {"OFF": 0, "NORMAL": 1}[synchronous]:
            raise RuntimeError("SQLite synchronous setting did not match its durability contract")
        result = {"journal_mode": mode, "synchronous": synchronous, "synchronous_value": value,
                  "sqlite_version": sqlite3.sqlite_version, "wal_reset_fixed": True,
                  "durability_scope": "process_crash_consistent; power_loss_may_lose_recent_commits" if synchronous == "NORMAL"
                      else "unsafe_on_process_or_power_failure"}
    record_sqlite_startup(result)
    return result
