from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.db.sqlite_observability import record_database_lock_error


def _engine_kwargs(database_url: str) -> dict:
    kwargs: dict = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": max(settings.sqlite_busy_timeout_ms, 100) / 1000}
    return kwargs


engine = create_async_engine(settings.database_url, **_engine_kwargs(settings.database_url))


@event.listens_for(engine.sync_engine, "handle_error")
def _observe_database_error(exception_context) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    if "database is locked" in str(exception_context.original_exception).lower():
        record_database_lock_error()


@event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={max(settings.sqlite_busy_timeout_ms, 100)}")
        cursor.execute("PRAGMA foreign_keys=ON")
        if ":memory:" not in settings.database_url:
            # journal_mode is a database-level transition.  Running WAL mode
            # switching on every pooled connection races during startup and
            # can leave several aiosqlite workers stuck in btreeBeginTrans.
            # The additive migration/runner performs the one-time transition;
            # connection setup only applies per-connection pragmas here.
            # 只有 sandbox_fast 明确允许牺牲最后一批 fsync；严格对账模式
            # 保持 NORMAL，避免配置误切后仍悄悄扩大崩溃丢失边界。
            synchronous = "OFF" if settings.exchange_durability_mode == "sandbox_fast" else "NORMAL"
            cursor.execute(f"PRAGMA synchronous={synchronous}")
            cursor.execute("PRAGMA wal_autocheckpoint=2000")
            cursor.execute("PRAGMA cache_size=-20000")
            cursor.execute("PRAGMA page_size")
            page_size = int(cursor.fetchone()[0])
            cursor.execute(f"PRAGMA max_page_count={max(1, settings.sqlite_control_hard_limit_bytes // page_size)}")
            cursor.execute("PRAGMA mmap_size=268435456")
    finally:
        cursor.close()


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
