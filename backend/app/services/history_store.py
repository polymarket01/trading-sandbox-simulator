"""Bounded, low-frequency SQLite store for sandbox market history.

This store is intentionally not part of the matching/event-sourcing session.
It owns its connection, queue and retention policy so a slow or full history
database cannot make the order book or WebSocket path wait on SQLite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.core.config import settings


logger = logging.getLogger("history_store")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS price_samples_1s (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    market_id INTEGER,
    symbol TEXT NOT NULL,
    sampled_at_ms INTEGER NOT NULL,
    bid TEXT,
    ask TEXT,
    mid TEXT NOT NULL,
    source TEXT NOT NULL,
    source_timestamp_ms INTEGER NOT NULL,
    carried_forward INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, symbol, sampled_at_ms)
);
CREATE INDEX IF NOT EXISTS idx_price_samples_1s_lookup
    ON price_samples_1s(run_id, symbol, sampled_at_ms);

CREATE TABLE IF NOT EXISTS sampled_klines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    market_id INTEGER,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    volume TEXT NOT NULL DEFAULT '0',
    quote_volume TEXT NOT NULL DEFAULT '0',
    trade_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'sampled_mid',
    carried_forward INTEGER NOT NULL DEFAULT 0,
    display_only INTEGER NOT NULL DEFAULT 1,
    sampled INTEGER NOT NULL DEFAULT 1,
    is_closed INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, symbol, interval, open_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_sampled_klines_lookup
    ON sampled_klines(run_id, symbol, interval, open_time_ms);

CREATE TABLE IF NOT EXISTS runtime_minute_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    minute_open_time_ms INTEGER NOT NULL,
    quote_cycles INTEGER NOT NULL DEFAULT 0,
    place_count INTEGER NOT NULL DEFAULT 0,
    amend_count INTEGER NOT NULL DEFAULT 0,
    cancel_count INTEGER NOT NULL DEFAULT 0,
    synthetic_fill_count INTEGER NOT NULL DEFAULT 0,
    display_fill_count INTEGER NOT NULL DEFAULT 0,
    actual_match_count INTEGER NOT NULL DEFAULT 0,
    stp_intercept_count INTEGER NOT NULL DEFAULT 0,
    command_latency_p50_ms REAL NOT NULL DEFAULT 0,
    command_latency_p95_ms REAL NOT NULL DEFAULT 0,
    command_latency_p99_ms REAL NOT NULL DEFAULT 0,
    publish_latency_p50_ms REAL NOT NULL DEFAULT 0,
    publish_latency_p95_ms REAL NOT NULL DEFAULT 0,
    publish_latency_p99_ms REAL NOT NULL DEFAULT 0,
    ws_drop_count INTEGER NOT NULL DEFAULT 0,
    ws_timeout_count INTEGER NOT NULL DEFAULT 0,
    cpu_pct REAL NOT NULL DEFAULT 0,
    rss_bytes INTEGER NOT NULL DEFAULT 0,
    footprint_bytes INTEGER NOT NULL DEFAULT 0,
    queue_depth INTEGER NOT NULL DEFAULT 0,
    queue_age_ms REAL NOT NULL DEFAULT 0,
    kline_freshness_ms INTEGER NOT NULL DEFAULT 0,
    sampling_drops INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    UNIQUE(run_id, symbol, minute_open_time_ms)
);
CREATE INDEX IF NOT EXISTS idx_runtime_minute_summaries_lookup
    ON runtime_minute_summaries(run_id, symbol, minute_open_time_ms);

CREATE TABLE IF NOT EXISTS error_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    category TEXT NOT NULL,
    error_code TEXT,
    message TEXT NOT NULL,
    symbol TEXT,
    occurred_at_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_error_samples_category
    ON error_samples(run_id, category, occurred_at_ms);

CREATE TABLE IF NOT EXISTS run_summaries (
    run_id TEXT PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER,
    persistence_mode TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS reconciliation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    category TEXT NOT NULL,
    observed_at_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_reconciliation_summaries_lookup
    ON reconciliation_summaries(run_id, category, observed_at_ms);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _as_bool(value: object) -> int:
    return 1 if bool(value) else 0


class HistoryStore:
    """A bounded writer and query facade for ``market_history.db``."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_id: str,
        data_root: str | Path | None = None,
        queue_max: int | None = None,
        batch_size: int | None = None,
        commit_interval_seconds: float | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        self.data_root = Path(data_root or self.path.parent).expanduser().resolve()
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(1, int(queue_max if queue_max is not None else settings.history_queue_max))
        )
        self.batch_size = max(1, int(batch_size if batch_size is not None else settings.history_batch_size))
        self.commit_interval_seconds = max(
            0.1,
            float(
                commit_interval_seconds
                if commit_interval_seconds is not None
                else settings.history_commit_interval_seconds
            ),
        )
        self._kline_generations: dict[str, int] = {}
        self._writer_task: asyncio.Task | None = None
        self._stop_requested = False
        self._writing = False
        self._lock = threading.RLock()
        self._metrics: dict[str, Any] = {
            "status": "starting",
            "storage_degraded": False,
            "sampling_paused": False,
            "sampling_drops": 0,
            "write_failures": 0,
            "written_records": 0,
            "last_commit_at": None,
            "last_error": None,
            "last_capacity_check": None,
            "last_rotation": None,
            "last_prune": None,
        }

    # ----- lifecycle -------------------------------------------------

    async def start(self) -> None:
        await asyncio.to_thread(self._init_schema_sync)
        self._stop_requested = False
        self._writer_task = asyncio.create_task(self._writer_loop(), name="history-store-writer")
        self._metrics.update({"status": "running", "storage_degraded": False, "sampling_paused": False})

    async def stop(self, *, flush: bool = True) -> None:
        if flush:
            await self.flush()
        self._stop_requested = True
        task = self._writer_task
        self._writer_task = None
        if task is not None:
            task.cancel()
            with contextlib_suppress(asyncio.CancelledError):
                await task
        self._metrics["status"] = "stopped"

    async def flush(self) -> None:
        while not self.queue.empty() or self._writing:
            await asyncio.sleep(0.01)
        self._metrics["last_commit_at"] = datetime.now(tz=UTC).isoformat()

    async def _writer_loop(self) -> None:
        while not self._stop_requested:
            try:
                first = await asyncio.wait_for(self.queue.get(), timeout=self.commit_interval_seconds)
            except asyncio.TimeoutError:
                continue
            batch = [first]
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._writing = True
            try:
                await asyncio.to_thread(self._write_records_sync, batch)
            except Exception as exc:  # storage failure is deliberately lossy
                self._metrics["write_failures"] += 1
                self._metrics["sampling_drops"] += len(batch)
                self._metrics["last_error"] = str(exc)[:500]
                logger.warning("history batch dropped size=%s: %s", len(batch), exc)
            finally:
                self._writing = False
                for _ in batch:
                    self.queue.task_done()

    # ----- enqueue ---------------------------------------------------

    def enqueue(self, record: dict[str, Any]) -> bool:
        if self._metrics.get("sampling_paused"):
            self._metrics["sampling_drops"] += 1
            return False
        item = dict(record)
        item.setdefault("run_id", self.run_id)
        if item.get("kind") == "kline":
            symbol = str(item.get("payload", {}).get("symbol", "")).upper()
            item["kline_generation"] = self._kline_generations.get(symbol, 0)
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._metrics["sampling_drops"] += 1
            self._metrics["last_error"] = "history_queue_full"
            return False
        return True

    def enqueue_kline(self, payload: dict[str, Any]) -> bool:
        return self.enqueue({"kind": "kline", "payload": payload})

    def enqueue_price_sample(self, payload: dict[str, Any]) -> bool:
        return self.enqueue({"kind": "price_sample", "payload": payload})

    def enqueue_minute_summary(self, payload: dict[str, Any]) -> bool:
        return self.enqueue({"kind": "minute_summary", "payload": payload})

    def enqueue_error(self, category: str, message: str, *, code: str | None = None, symbol: str | None = None, payload: dict | None = None) -> bool:
        return self.enqueue(
            {
                "kind": "error",
                "payload": {
                    "category": str(category),
                    "message": str(message)[:2000],
                    "error_code": code,
                    "symbol": symbol,
                    "occurred_at_ms": _now_ms(),
                    "payload": payload or {},
                },
            }
        )

    def enqueue_run_summary(self, payload: dict[str, Any]) -> bool:
        return self.enqueue({"kind": "run_summary", "payload": payload})

    def enqueue_reconciliation(self, payload: dict[str, Any]) -> bool:
        return self.enqueue({"kind": "reconciliation", "payload": payload})

    # ----- sqlite writes/reads --------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        return conn

    def _init_schema_sync(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                conn.commit()
            finally:
                conn.close()

    def _write_records_sync(self, records: Iterable[dict[str, Any]]) -> None:
        rows = list(records)
        if not rows:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                for record in rows:
                    kind = str(record.get("kind") or "")
                    payload = dict(record.get("payload") or {})
                    if kind == "price_sample":
                        self._upsert_price_sample(conn, payload)
                    elif kind == "kline":
                        symbol = str(payload.get("symbol", "")).upper()
                        if record.get("kline_generation", 0) == self._kline_generations.get(symbol, 0):
                            self._upsert_kline(conn, payload)
                    elif kind == "minute_summary":
                        self._upsert_minute_summary(conn, payload)
                    elif kind == "error":
                        self._insert_error(conn, payload)
                    elif kind == "run_summary":
                        self._upsert_run_summary(conn, payload)
                    elif kind == "reconciliation":
                        self._insert_reconciliation(conn, payload)
                conn.commit()
            finally:
                conn.close()
        self._metrics["written_records"] += len(rows)
        self._metrics["last_commit_at"] = datetime.now(tz=UTC).isoformat()

    def write_records_sync(self, records: Iterable[dict[str, Any]]) -> None:
        """Synchronous hook used by isolated tests and migration checks."""
        self._write_records_sync(records)

    def _upsert_kline(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO sampled_klines
            (run_id, market_id, symbol, interval, open_time_ms, close_time_ms,
             open, high, low, close, volume, quote_volume, trade_count, source,
             carried_forward, display_only, sampled, is_closed, updated_at_ms, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, symbol, interval, open_time_ms) DO UPDATE SET
              market_id=excluded.market_id, close_time_ms=excluded.close_time_ms,
              open=excluded.open, high=excluded.high, low=excluded.low,
              close=excluded.close, volume=excluded.volume,
              quote_volume=excluded.quote_volume, trade_count=excluded.trade_count,
              source=excluded.source, carried_forward=excluded.carried_forward,
              display_only=excluded.display_only, sampled=excluded.sampled,
              is_closed=excluded.is_closed, updated_at_ms=excluded.updated_at_ms,
              payload_json=excluded.payload_json""",
            (
                str(p.get("run_id") or self.run_id),
                p.get("market_id"),
                str(p.get("symbol") or "").upper(),
                str(p.get("interval") or "1m"),
                int(p.get("open_time_ms") or 0),
                int(p.get("close_time_ms") or 0),
                str(p.get("open") or "0"),
                str(p.get("high") or "0"),
                str(p.get("low") or "0"),
                str(p.get("close") or "0"),
                str(p.get("volume") or "0"),
                str(p.get("quote_volume") or "0"),
                int(p.get("trade_count") or 0),
                str(p.get("source") or "sampled_mid"),
                _as_bool(p.get("carried_forward")),
                _as_bool(p.get("display_only", True)),
                _as_bool(p.get("sampled", True)),
                _as_bool(p.get("is_closed")),
                int(p.get("updated_at_ms") or _now_ms()),
                _json(p.get("payload") or {}),
            ),
        )

    def _upsert_price_sample(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO price_samples_1s
            (run_id, market_id, symbol, sampled_at_ms, bid, ask, mid, source,
             source_timestamp_ms, carried_forward)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, symbol, sampled_at_ms) DO UPDATE SET
              market_id=excluded.market_id, bid=excluded.bid, ask=excluded.ask,
              mid=excluded.mid, source=excluded.source,
              source_timestamp_ms=excluded.source_timestamp_ms,
              carried_forward=excluded.carried_forward""",
            (
                str(p.get("run_id") or self.run_id),
                p.get("market_id"),
                str(p.get("symbol") or "").upper(),
                int(p.get("sampled_at_ms") or _now_ms()),
                None if p.get("bid") is None else str(p.get("bid")),
                None if p.get("ask") is None else str(p.get("ask")),
                str(p.get("mid") or "0"),
                str(p.get("source") or "sampled_mid"),
                int(p.get("source_timestamp_ms") or p.get("sampled_at_ms") or _now_ms()),
                _as_bool(p.get("carried_forward")),
            ),
        )

    def _upsert_minute_summary(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        columns = [
            "quote_cycles", "place_count", "amend_count", "cancel_count",
            "synthetic_fill_count", "display_fill_count", "actual_match_count",
            "stp_intercept_count", "command_latency_p50_ms", "command_latency_p95_ms",
            "command_latency_p99_ms", "publish_latency_p50_ms", "publish_latency_p95_ms",
            "publish_latency_p99_ms", "ws_drop_count", "ws_timeout_count", "cpu_pct",
            "rss_bytes", "footprint_bytes", "queue_depth", "queue_age_ms",
            "kline_freshness_ms", "sampling_drops",
        ]
        values = [p.get(column, 0) for column in columns]
        conn.execute(
            f"""INSERT INTO runtime_minute_summaries
            (run_id, symbol, minute_open_time_ms, {', '.join(columns)}, payload_json, created_at_ms)
            VALUES (?, ?, ?, {', '.join('?' for _ in columns)}, ?, ?)
            ON CONFLICT(run_id, symbol, minute_open_time_ms) DO UPDATE SET
              {', '.join(f'{column}=excluded.{column}' for column in columns)},
              payload_json=excluded.payload_json, created_at_ms=excluded.created_at_ms""",
            [
                str(p.get("run_id") or self.run_id),
                str(p.get("symbol") or "").upper(),
                int(p.get("minute_open_time_ms") or 0),
                *values,
                _json(p.get("payload") or {}),
                int(p.get("created_at_ms") or _now_ms()),
            ],
        )

    def _insert_error(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO error_samples(run_id, category, error_code, message, symbol, occurred_at_ms, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(p.get("run_id") or self.run_id),
                str(p.get("category") or "unknown"),
                p.get("error_code"),
                str(p.get("message") or "")[:2000],
                p.get("symbol"),
                int(p.get("occurred_at_ms") or _now_ms()),
                _json(p.get("payload") or {}),
            ),
        )

    def _upsert_run_summary(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        run_id = str(p.get("run_id") or self.run_id)
        conn.execute(
            """INSERT INTO run_summaries(run_id, started_at_ms, ended_at_ms, persistence_mode, status, summary_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET ended_at_ms=excluded.ended_at_ms,
              persistence_mode=excluded.persistence_mode, status=excluded.status,
              summary_json=excluded.summary_json""",
            (
                run_id,
                int(p.get("started_at_ms") or _now_ms()),
                p.get("ended_at_ms"),
                str(p.get("persistence_mode") or "sampled"),
                str(p.get("status") or "running"),
                _json(p.get("summary") or p),
            ),
        )

    def _insert_reconciliation(self, conn: sqlite3.Connection, p: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO reconciliation_summaries(run_id, category, observed_at_ms, status, summary_json) VALUES (?, ?, ?, ?, ?)",
            (
                str(p.get("run_id") or self.run_id),
                str(p.get("category") or "runtime"),
                int(p.get("observed_at_ms") or _now_ms()),
                str(p.get("status") or "unknown"),
                _json(p.get("summary") or p),
            ),
        )

    async def clear_market_klines(self, symbol: str) -> int:
        """Invalidate queued candles and delete only this market's chart history."""
        symbol = symbol.upper()
        self._kline_generations[symbol] = self._kline_generations.get(symbol, 0) + 1
        return await asyncio.to_thread(self._clear_market_klines_sync, symbol)

    def _clear_market_klines_sync(self, symbol: str) -> int:
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute("DELETE FROM sampled_klines WHERE symbol=?", (symbol,))
                conn.commit()
                return result.rowcount
            finally:
                conn.close()

    async def query_klines(self, symbol: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self.query_klines_sync, symbol, interval, limit)

    def query_klines_sync(self, symbol: str, interval: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                rows = conn.execute(
                    """SELECT market_id, symbol, interval, open_time_ms, close_time_ms,
                    open, high, low, close, volume, quote_volume, trade_count, source,
                    carried_forward, display_only, sampled, is_closed, updated_at_ms, payload_json
                    FROM sampled_klines WHERE run_id=? AND symbol=? AND interval=?
                    ORDER BY open_time_ms DESC LIMIT ?""",
                    (self.run_id, str(symbol).upper(), str(interval), max(1, int(limit))),
                ).fetchall()
            finally:
                conn.close()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            payload = {}
            try:
                payload = json.loads(row[18] or "{}")
            except (TypeError, ValueError):
                pass
            result.append(
                {
                    "market_id": row[0], "symbol": row[1], "interval": row[2],
                    "open_time": row[3], "close_time": row[4], "open": row[5],
                    "high": row[6], "low": row[7], "close": row[8], "volume": row[9],
                    "quote_volume": row[10], "trade_count": row[11], "source": row[12],
                    "carried_forward": bool(row[13]), "display_only": bool(row[14]),
                    "sampled": bool(row[15]), "is_closed": bool(row[16]),
                    "updated_at_ms": row[17], **payload,
                }
            )
        return result

    # ----- retention and capacity -----------------------------------

    def usage_bytes(self) -> dict[str, int]:
        paths = {"db": self.path, "wal": Path(f"{self.path}-wal"), "shm": Path(f"{self.path}-shm")}
        values = {name: int(path.stat().st_size) if path.exists() else 0 for name, path in paths.items()}
        values["total"] = sum(values.values())
        return values

    def data_root_usage_bytes(self) -> int:
        total = 0
        if not self.data_root.exists():
            return 0
        for path in self.data_root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += int(path.stat().st_size)
            except OSError:
                continue
        return total

    async def enforce_capacity(self, *, maker_quiesced: bool) -> dict[str, Any]:
        result = await asyncio.to_thread(self.enforce_capacity_sync, maker_quiesced=maker_quiesced)
        self._metrics["last_capacity_check"] = result
        return result

    def enforce_capacity_sync(self, *, maker_quiesced: bool) -> dict[str, Any]:
        usage = self.usage_bytes()
        data_usage = self.data_root_usage_bytes()
        soft = int(settings.history_db_soft_limit_bytes)
        hard = int(settings.history_db_hard_limit_bytes)
        triggered = usage["total"] >= soft or data_usage >= int(settings.sandbox_data_max_bytes)
        prune = {"klines": 0, "minute_summaries": 0, "errors": 0}
        if triggered:
            prune = self.prune_sync()
            usage = self.usage_bytes()
            data_usage = self.data_root_usage_bytes()
        hard_hit = usage["total"] >= hard or usage["wal"] >= int(settings.history_db_wal_limit_bytes) or data_usage >= int(settings.sandbox_data_max_bytes)
        if hard_hit:
            self._metrics["storage_degraded"] = True
            self._metrics["sampling_paused"] = True
            self._metrics["status"] = "storage_degraded"
            rotation = self._rotate_sync() if maker_quiesced else {"status": "deferred", "reason": "active_maker"}
        else:
            rotation = {"status": "not_needed"}
        return {
            "status": "storage_degraded" if hard_hit else "healthy",
            "triggered": triggered,
            "hard_limit_hit": hard_hit,
            "maker_quiesced": bool(maker_quiesced),
            "usage": usage,
            "data_root_usage_bytes": data_usage,
            "soft_limit_bytes": soft,
            "hard_limit_bytes": hard,
            "wal_limit_bytes": int(settings.history_db_wal_limit_bytes),
            "prune": prune,
            "rotation": rotation,
        }

    def prune_sync(self) -> dict[str, int]:
        now_ms = _now_ms()
        counts = {"price_samples": 0, "klines": 0, "minute_summaries": 0, "errors": 0}
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(SCHEMA_SQL)
                cursor = conn.execute(
                    "DELETE FROM price_samples_1s WHERE run_id=? AND sampled_at_ms < ?",
                    (self.run_id, now_ms - 24 * 60 * 60 * 1000),
                )
                counts["price_samples"] = int(cursor.rowcount or 0)
                keep_by_interval = {
                    "1m": int(settings.history_1m_keep_per_market),
                    "5m": int(settings.history_5m_keep_per_market),
                    "1h": int(settings.history_1h_keep_per_market),
                }
                for interval, keep in keep_by_interval.items():
                    if keep < 0:
                        continue
                    symbols = [
                        str(row[0])
                        for row in conn.execute(
                            "SELECT DISTINCT symbol FROM sampled_klines WHERE run_id=? AND interval=?",
                            (self.run_id, interval),
                        ).fetchall()
                    ]
                    for symbol in symbols:
                        cursor = conn.execute(
                            """DELETE FROM sampled_klines WHERE id IN (
                                SELECT id FROM sampled_klines
                                WHERE run_id=? AND symbol=? AND interval=?
                                ORDER BY open_time_ms DESC LIMIT -1 OFFSET ?
                            )""",
                            (self.run_id, symbol, interval, max(0, keep)),
                        )
                        counts["klines"] += int(cursor.rowcount or 0)
                minute_keep = max(0, int(settings.history_minute_summary_keep))
                cursor = conn.execute(
                    """DELETE FROM runtime_minute_summaries WHERE id IN (
                        SELECT id FROM runtime_minute_summaries WHERE run_id=?
                        ORDER BY minute_open_time_ms DESC LIMIT -1 OFFSET ?
                    ) OR minute_open_time_ms < ?""",
                    (self.run_id, minute_keep, now_ms - int(settings.history_minute_summary_max_age_days) * 86_400_000),
                )
                counts["minute_summaries"] = int(cursor.rowcount or 0)
                for category_row in conn.execute("SELECT DISTINCT category FROM error_samples WHERE run_id=?", (self.run_id,)):
                    category = str(category_row[0])
                    keep_errors = max(0, int(settings.history_error_keep_per_category))
                    cursor = conn.execute(
                        """DELETE FROM error_samples WHERE id IN (
                            SELECT id FROM error_samples WHERE run_id=? AND category=?
                            ORDER BY occurred_at_ms DESC LIMIT -1 OFFSET ?
                        )""",
                        (self.run_id, category, keep_errors),
                    )
                    counts["errors"] += int(cursor.rowcount or 0)
                conn.commit()
            finally:
                conn.close()
        self._metrics["last_prune"] = counts
        return counts

    def checkpoint_if_quiesced(self, *, maker_quiesced: bool) -> dict[str, Any]:
        if not maker_quiesced:
            return {"status": "deferred", "reason": "active_maker"}
        with self._lock:
            conn = self._connect()
            try:
                result = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                conn.commit()
            finally:
                conn.close()
        return {"status": "checkpointed", "result": list(result or [])}

    def _rotate_sync(self) -> dict[str, Any]:
        """Rotate only this exact sandbox history file; never recursive-delete."""
        resolved_path = self.path.resolve()
        root = self.data_root.resolve()
        if resolved_path.parent != root or resolved_path.name != "market_history.db":
            return {"status": "refused", "reason": "path_not_explicit_sandbox_history_db"}
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
            finally:
                conn.close()
            if not self.path.exists():
                self._init_schema_sync()
                return {"status": "not_found"}
            stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
            suffix = self.run_id if _SAFE_NAME.fullmatch(self.run_id) else "run"
            archive = self.path.with_name(f"market_history.db.archive.{stamp}.{suffix}")
            os.replace(self.path, archive)
            for sidecar in (Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                if sidecar.exists() and sidecar.parent == root:
                    sidecar.unlink()
            self._init_schema_sync()
            archives = sorted(root.glob("market_history.db.archive.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in archives[max(1, int(settings.history_rotation_keep_archives)) :]:
                if old.is_file() and old.parent == root:
                    old.unlink()
        self._metrics["last_rotation"] = str(archive)
        return {"status": "rotated", "archive": str(archive)}

    def metrics_snapshot(self) -> dict[str, Any]:
        result = dict(self._metrics)
        result["queue_depth"] = self.queue.qsize()
        result["queue_max"] = self.queue.maxsize
        result["usage"] = self.usage_bytes()
        result["data_root_usage_bytes"] = self.data_root_usage_bytes()
        result["mode"] = "sampled"
        result["soft_limit_bytes"] = int(settings.history_db_soft_limit_bytes)
        result["hard_limit_bytes"] = int(settings.history_db_hard_limit_bytes)
        result["wal_limit_bytes"] = int(settings.history_db_wal_limit_bytes)
        result["cap_bytes"] = int(settings.history_db_hard_limit_bytes)
        return result


class contextlib_suppress:
    """Tiny local suppress helper to keep this module dependency-free."""

    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and any(issubclass(exc_type, item) for item in self.exceptions)
