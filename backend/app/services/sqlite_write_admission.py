"""Bounded SQLite write admission before an in-memory financial mutation."""
from __future__ import annotations

import asyncio

from sqlalchemy.exc import OperationalError

from app.services.matching_faults import NotExecuted


class WriteAdmissionRejected(NotExecuted):
    """The caller has not submitted its matching command yet."""

    definitive = True


async def acquire_sqlite_write_admission(
    session, *, timeout_ms: int = 100, deadline_ms: int = 1000,
    existing_order_id: str | None = None,
) -> bool:
    """Acquire write ownership without committing, rolling back or expiring ORM.

    New orders use BEGIN IMMEDIATE before the transaction's first write/BEGIN.
    Cancel/amend can supply an existing order ID: a SQLite-only no-op UPDATE
    acquires/proves write ownership, including in an existing transaction.
    Neither form flushes pending ORM changes. Already-written callers need no
    admission. Legacy SELECTs permit BEGIN IMMEDIATE in a logical transaction.
    The existing caller remains responsible for commit/rollback and lock order.
    timeout_ms caps SQLite's actual busy wait; deadline_ms separately bounds
    pool setup and all SQL/event-loop handoffs. Cleanup may finish later, but
    an expired or cancelled admission can never proceed into matching.
    """
    if session.get_bind().dialect.name != "sqlite":
        return False
    transaction = session.sync_session.get_transaction()
    if transaction is not None and session.info.get("sqlite_write_admission") is transaction:
        return True
    deadline = asyncio.get_running_loop().time() + max(1, int(deadline_ms)) / 1000
    try:
        async with asyncio.timeout_at(deadline):
            result = await _acquire(session, timeout_ms, existing_order_id)
        # A synchronous callback can delay the event loop past the timer's
        # deadline without giving cancellation a chance to run first.
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
    except TimeoutError as exc:
        raise WriteAdmissionRejected(
            "STORAGE_ADMISSION_TIMEOUT: write admission timed out; matching command not executed"
        ) from exc
    session.info["sqlite_write_admission"] = session.sync_session.get_transaction()
    return result


async def _restore_busy_timeout(connection, previous):
    if connection.invalidated:
        return
    statement = f"PRAGMA busy_timeout={previous}"
    try:
        await connection.exec_driver_sql(statement)
    except asyncio.CancelledError:
        # SQLAlchemy usually invalidates an interrupted driver operation. If
        # cancellation happened before that operation began, restore the pool
        # connection's original setting before preserving the cancellation.
        if not connection.invalidated:
            cleanup = asyncio.create_task(connection.exec_driver_sql(statement))
            while True:
                try:
                    await asyncio.shield(cleanup)
                    break
                except asyncio.CancelledError:
                    if cleanup.done():
                        cleanup.result()
                        break
        raise


async def _acquire(session, timeout_ms, existing_order_id):
    connection = await session.connection()
    previous = int((await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar_one())
    budget = max(0, min(int(timeout_ms), previous))
    try:
        await connection.exec_driver_sql(f"PRAGMA busy_timeout={budget}")
        try:
            if existing_order_id is None:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                await connection.exec_driver_sql(
                    "UPDATE orders SET version=version WHERE order_id=?", (str(existing_order_id),)
                )
        except OperationalError as exc:
            code = getattr(exc.orig, "sqlite_errorcode", None)
            if code is not None and (code & 0xFF) in {5, 6}:
                raise WriteAdmissionRejected("STORAGE_BUSY: matching command not executed") from exc
            if "cannot start a transaction within a transaction" in str(exc.orig).lower():
                raise WriteAdmissionRejected(
                    "STORAGE_TRANSACTION_OPEN: write admission requires a fresh transaction; matching command not executed"
                ) from exc
            raise
        return True
    finally:
        # A cancelled statement invalidates its aiosqlite connection. Avoid
        # masking that cancellation by issuing SQL on an invalid transaction.
        await _restore_busy_timeout(connection, previous)
