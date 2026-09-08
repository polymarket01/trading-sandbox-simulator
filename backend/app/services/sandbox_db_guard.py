from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.domain_event_log import DomainEventLog, DomainEventWatermark
from app.models.exchange_runtime import ExchangeSnapshotRecord
from app.services.history_retention_service import HistoryRetentionService

logger = logging.getLogger("sandbox_db_guard")

PRUNE_BATCH = 20_000


async def prune_active_history(session_factory) -> dict[str, Any]:
    """Small online transaction; never VACUUM or touch accounting facts."""
    async with session_factory() as session:
        result = await session.execute(text("""
            DELETE FROM domain_event_log WHERE id IN (
              SELECT id FROM domain_event_log
              WHERE state='applied' AND id <= COALESCE(
                (SELECT materialized_seq FROM domain_event_watermark WHERE id=1),0)
              ORDER BY id DESC LIMIT 2000 OFFSET 5000)
        """))
        deleted = int(result.rowcount or 0)
        rejected = await session.execute(text("""
            DELETE FROM causal_command_journal WHERE command_id IN (
              SELECT c.command_id FROM causal_command_journal c
              WHERE c.command_type='QUOTE_SET_REPLACE' AND c.status='REJECTED'
              AND c.updated_at < datetime('now','-1 day')
              AND NOT EXISTS (SELECT 1 FROM causal_execution_bundle b WHERE b.command_id=c.command_id)
              ORDER BY c.updated_at DESC LIMIT 500 OFFSET 1000)
        """))
        await session.commit()
        return {"status": "online_pruned", "events": deleted,
                "rejected_quotes": int(rejected.rowcount or 0), "automatic_vacuum": False}


def database_file_path(database_url: str | None = None) -> Path | None:
    """Return the SQLite file path for the configured URL, or None for non-SQLite."""
    url = database_url or settings.database_url
    lowered = str(url).lower()
    if not lowered.startswith("sqlite"):
        return None
    if ":///" in str(url):
        path = str(url).split(":///", 1)[1]
    elif "://" in str(url):
        path = str(url).split("://", 1)[1]
    else:
        path = str(url)
    if not path or path in {":memory:", ""}:
        return None
    return Path(path)


def file_size_bytes(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    return int(path.stat().st_size)


async def prune_sandbox_db(
    session: AsyncSession,
    *,
    max_bytes: int | None = None,
    keep_bytes: int | None = None,
    keep_events: int | None = None,
    keep_snapshots: int | None = None,
    dry_run: bool = False,
    maker_quiesced: bool = True,
) -> dict[str, Any]:
    """Prune sandbox-only history so the local SQLite file stays near a cap.

    Safe targets only:
      * applied ``domain_event_log`` rows already materialized (keeps a small
        diagnostic tail);
      * old ``exchange_snapshot_record`` metadata;
      * non-customer order/trade/kline history via HistoryRetentionService
        (financial pruning stays fail-closed by default).
    The file is then check-pointed and VACUUMed so the on-disk size shrinks.
    """
    max_bytes = int(max_bytes if max_bytes is not None else settings.sandbox_db_guard_max_bytes)
    keep_bytes = int(keep_bytes if keep_bytes is not None else settings.sandbox_db_guard_keep_bytes)
    keep_events = int(keep_events if keep_events is not None else settings.sandbox_db_guard_keep_events)
    keep_snapshots = int(keep_snapshots if keep_snapshots is not None else settings.sandbox_db_guard_keep_snapshots)
    db_path = database_file_path()
    before = file_size_bytes(db_path)
    if before < max_bytes:
        return {
            "triggered": False,
            "max_bytes": max_bytes,
            "keep_bytes": keep_bytes,
            "size_bytes": before,
            "deleted": {"events": 0, "snapshots": 0},
            "retention": {},
            "vacuum": {"ok": True, "skipped": True},
        }

    watermark = int(
        await session.scalar(
            select(DomainEventWatermark.materialized_seq).where(DomainEventWatermark.id == 1)
        )
        or 0
    )

    deleted_events = 0
    if not dry_run:
        while True:
            candidate_ids = (
                select(DomainEventLog.id)
                .where(
                    DomainEventLog.state == "applied",
                    DomainEventLog.id <= watermark,
                )
                .order_by(DomainEventLog.id.desc())
                .offset(keep_events)
                .limit(PRUNE_BATCH)
            )
            result = await session.execute(
                delete(DomainEventLog).where(DomainEventLog.id.in_(candidate_ids))
            )
            deleted = int(result.rowcount or 0)
            deleted_events += deleted
            if deleted < PRUNE_BATCH:
                break

    deleted_snapshots = 0
    if not dry_run:
        keep_ids = (
            select(ExchangeSnapshotRecord.id)
            .order_by(ExchangeSnapshotRecord.id.desc())
            .limit(keep_snapshots)
        )
        result = await session.execute(
            delete(ExchangeSnapshotRecord).where(ExchangeSnapshotRecord.id.not_in(keep_ids))
        )
        deleted_snapshots = int(result.rowcount or 0)

    retention_result = await HistoryRetentionService().prune(session, dry_run=dry_run)
    if not dry_run:
        await session.commit()

    vacuum: dict[str, Any] = {"ok": True, "skipped": True}
    if not dry_run and maker_quiesced:
        vacuum = await _shrink_sqlite_file(db_path)
    elif not maker_quiesced and not dry_run:
        vacuum = {"ok": True, "skipped": True, "reason": "active_maker"}
    after = file_size_bytes(db_path)
    return {
        "triggered": True,
        "max_bytes": max_bytes,
        "keep_bytes": keep_bytes,
        "watermark": watermark,
        "size_bytes": after,
        "size_before_bytes": before,
        "deleted": {"events": deleted_events, "snapshots": deleted_snapshots},
        "retention": retention_result,
        "vacuum": vacuum,
    }


async def run_sandbox_db_size_guard(
    session_factory: async_sessionmaker,
    *,
    max_bytes: int | None = None,
    keep_bytes: int | None = None,
    keep_events: int | None = None,
    keep_snapshots: int | None = None,
    maker_quiesced: bool = True,
) -> dict[str, Any]:
    """Periodic entrypoint: only touches the DB when the file exceeds the cap."""
    max_bytes = int(max_bytes if max_bytes is not None else settings.sandbox_db_guard_max_bytes)
    db_path = database_file_path()
    if db_path is None or file_size_bytes(db_path) < max_bytes:
        return {
            "triggered": False,
            "max_bytes": max_bytes,
            "size_bytes": file_size_bytes(db_path),
            "deleted": {"events": 0, "snapshots": 0},
        }
    async with session_factory() as session:
        return await prune_sandbox_db(
            session,
            max_bytes=max_bytes,
            keep_bytes=keep_bytes,
            keep_events=keep_events,
            keep_snapshots=keep_snapshots,
            maker_quiesced=maker_quiesced,
        )


def _vacuum_sync(db_path: Path, *, attempts: int = 5) -> dict[str, Any]:
    """Checkpoint (TRUNCATE) and VACUUM a SQLite file from a worker thread."""
    last_error: str | None = None
    for attempt in range(attempts):
        try:
            conn = sqlite3.connect(str(db_path), timeout=30)
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "attempt": attempt, "size_bytes": file_size_bytes(db_path)}
        except Exception as exc:
            last_error = str(exc)
            if "locked" in last_error.lower() and attempt < attempts - 1:
                time.sleep(1.0 + attempt)
                continue
            break
    return {"ok": False, "attempt": attempts - 1, "error": (last_error or "unknown")[:300]}


async def _shrink_sqlite_file(db_path: Path) -> dict[str, Any]:
    if db_path is None:
        return {"ok": True, "skipped": True}
    return await asyncio.to_thread(_vacuum_sync, db_path)
