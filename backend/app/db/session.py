from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.sqlite_observability import record_connection_init_error, record_database_lock_error
from app.db.sqlite_startup import sqlite_synchronous_mode


def _engine_kwargs(database_url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": max(settings.sqlite_busy_timeout_ms, 100) / 1000}
        url = make_url(database_url)
        if (url.database and url.database != ":memory:" and url.query.get("mode") != "memory"
                and not url.database.startswith("file::memory:")):
            # Keep the existing 15-connection ceiling, retaining burst-created
            # workers instead of rebuilding them and their PRAGMAs each burst.
            # Connections remain lazy; memory databases keep their own pool.
            kwargs.update(pool_size=15, max_overflow=0)
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs(settings.database_url))


@event.listens_for(engine.sync_engine, "handle_error")
def _observe_database_error(exception_context) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    if "database is locked" in str(exception_context.original_exception).lower():
        record_database_lock_error(exception_context.original_exception)


async def _close_failed_driver(connection) -> None:
    # The failed checkout is not owned by a Session or the pool yet. Finish its
    # close even if the request is cancelled again while cleanup is in progress.
    close_task = asyncio.create_task(connection.close())
    while True:
        try:
            await asyncio.shield(close_task)
            return
        except asyncio.CancelledError:
            if close_task.done():
                close_task.result()
                return


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = None
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={max(settings.sqlite_busy_timeout_ms, 100)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        if ":memory:" not in settings.database_url:
            # journal_mode is a database-level transition.  Running WAL mode
            # switching on every pooled connection races during startup and
            # can leave several aiosqlite workers stuck in btreeBeginTrans.
            # The additive migration/runner performs the one-time transition;
            # connection setup only applies per-connection pragmas here.
            # Facts-durable never inherits sandbox_fast's unsafe OFF default.
            # NORMAL preserves the existing no-per-commit-fsync contract.
            synchronous = sqlite_synchronous_mode()
            cursor.execute(f"PRAGMA synchronous={synchronous}")
            cursor.execute("PRAGMA wal_autocheckpoint=2000")
            cursor.execute("PRAGMA cache_size=-20000")
            cursor.execute("PRAGMA page_size")
            page_size = int(cursor.fetchone()[0])
            cursor.execute(f"PRAGMA max_page_count={max(1, settings.sqlite_control_hard_limit_bytes // page_size)}")
            cursor.execute("PRAGMA mmap_size=268435456")
        cursor.close()
    except BaseException as exc:
        # SQLAlchemy's connect event can fail after aiosqlite started its
        # worker, before the connection is registered in the pool. Closing only
        # the cursor leaked one worker/FD per failed initialization.
        record_connection_init_error(exc)
        if cursor is not None:
            try:
                cursor.close()
            except BaseException:
                pass
        try:
            if hasattr(dbapi_connection, "run_async"):
                dbapi_connection.run_async(_close_failed_driver)
            else:
                dbapi_connection.close()
        except BaseException:
            logging.getLogger("sqlite").exception("failed to close rejected SQLite connection")
        raise


SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        try:
            yield session
        except IntegrityError as exc:
            await session.rollback()
            if any(name in str(exc.orig) for name in ('market_bot_accounts.user_id', 'uq_active_strategy_uid', 'users.id')):
                from fastapi import HTTPException
                raise HTTPException(409, '执行 UID 已被其他配置占用，请刷新后重试') from exc
            raise
