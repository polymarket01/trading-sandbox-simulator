from __future__ import annotations

import asyncio
from contextlib import contextmanager
import contextvars
from hashlib import sha256
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from typing import Callable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED
from app.core.config import settings
from app.models.domain_event_log import DomainEventLog, DomainEventWatermark
from app.models.market import Market
from app.models.order import Order
from app.models.user import User
from app.schemas.api import (
    ContractOrderAmendRequest,
    ContractOrderCreateRequest,
    OrderAmendRequest,
    OrderCreateRequest,
)
from app.services.contract_service import ContractService
from app.services.contract_service import ContractValidationError
from app.services.order_service import OrderService
from app.services.order_service import OrderValidationError
from app.services.state_snapshot import JournalTruncationGate
from app.services.persistence_contract import is_runtime_only_mode, legacy_sampled_runtime, platform_durable_contract

logger = logging.getLogger("persistence_writer")


class PersistenceWriter:
    """Event-sourcing write path for fast-path orders.

    Hot path:
      in-memory engine mutation -> enqueue domain event -> append-only
      `domain_event_log` rows (one JSON row per event, one transaction per
      batch).  The API acknowledges once the event is durably appended.

    Background materialization:
      a single worker applies pending events to business tables (orders,
      balances, contract positions, ledger, outbox) using the same replay
      functions as before, and advances the watermark.  Business tables are a
      slow materialized view; they never gate order acceptance.  On restart the
      writer first catch-up materializes pending events, so existing
      load-from-DB recovery still sees complete state.

    Crash boundary: every event produced by a real engine mutation is appended
    before it can become part of the durable view.  Restart-ephemeral FLOW is
    implemented before matching as a display-only preview and never reaches
    this writer.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        order_service: OrderService,
        contract_service: ContractService | None = None,
        *,
        max_queue: int = 60_000,
        batch_max: int = 64,
        batch_window_seconds: float = 0.02,
        materialize_interval_seconds: float = 0.3,
        materialize_batch_max: int = 256,
        retention_seconds: int = 7 * 86400,
    ) -> None:
        self._session_factory = session_factory
        self._order_service = order_service
        self._contract_service = contract_service
        self._max_queue = max_queue
        self._batch_max = batch_max
        self._batch_window_seconds = batch_window_seconds
        self._materialize_interval_seconds = materialize_interval_seconds
        self._materialize_batch_max = materialize_batch_max
        self._retention_seconds = retention_seconds
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue)
        self._append_lock = asyncio.Lock()
        self._append_jobs: set[asyncio.Task] = set()
        self._append_receipts: dict[int, asyncio.Future] = {}
        self._worker_task: asyncio.Task | None = None
        self._materializer_task: asyncio.Task | None = None
        self._seq = 0
        self._persisted_max_id = 0
        self._watermark = 0
        self._initialized = False
        self._metrics = {
            "enqueued": 0,
            "persisted": 0,
            "materialized": 0,
            "coalesced": 0,
            "failed": 0,
            "skipped": 0,
            "dead_letter": 0,
            "retention_removed": 0,
            "last_persisted_seq": 0,
            "last_materialized_seq": 0,
            "last_materialized_at": None,
            "queue_size": 0,
            "batch_count": 0,
            "materialize_batch_count": 0,
            "materialization_lag": 0,
            "ephemeral_quote_set_commands": 0,
            "ephemeral_quote_set_operations": 0,
            "robot_flow_persistence_dropped_events": 0,
            "robot_flow_persistence_dropped_fills": 0,
            "robot_flow_synthetic_events": 0,
            "robot_flow_synthetic_fills": 0,
            "robot_flow_synthetic_by_symbol": {},
            "maker_anchor_alignments": 0,
            "by_symbol": {},
            "applied_by_symbol": {},
            "by_kind": {},
            "sampled_in_memory_events": 0,
            "sampled_dropped_events": 0,
            "sampled_in_memory_commands": 0,
        }
        self._dead_letter: list[dict] = []
        self._stopping = False
        self._busy = False
        self._blocked_task: dict | None = None
        self._robot_user_ids: set[int] = set()
        self._critical_blocked: dict | None = None
        self._capture_var: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
            "persistence_writer_capture", default=None
        )
        self._ephemeral_quote_set_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "persistence_writer_ephemeral_quote_set", default=False
        )
        self._watermarks = {
            "ingress_seq": 0,
            "matched_seq": 0,
            "durable_seq": 0,
            "materialized_seq": 0,
            "published_seq": 0,
            "critical_materialized_seq": 0,
            "non_critical_materialized_seq": 0,
        }
        self._journal_commit_p99_ms = 0.0
        self._journal_commit_samples: list[float] = []
        self._metrics.update(
            {
                "journal_commands": 0,
                "captured_operations": 0,
                "critical_sink_failures": 0,
                "non_critical_sink_failures": 0,
                "last_journal_commit_ms": 0.0,
                "journal_commit_p99_ms": 0.0,
                "durability_mode": settings.exchange_durability_mode,
                "status": "STARTING",
            }
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._run(), name="persistence-writer")
        if self._materializer_task is None or self._materializer_task.done():
            self._materializer_task = asyncio.create_task(
                self._materialize_loop(), name="persistence-materializer"
            )
        # A sampled run can be completely idle from the durable event
        # writer's perspective: robot quotes and balances stay in memory and
        # no materialization batch is required to prove liveness.  Leaving
        # the constructor's STARTING marker in that case makes /health lie
        # forever even though both writer tasks are alive and unblocked.
        if self._critical_blocked is None:
            self._metrics["status"] = "HEALTHY"

    async def initialize(self) -> None:
        """Load the persisted watermark and max event id once at startup."""
        if self._initialized:
            return
        async with self._session_factory() as session:
            watermark = await self._ensure_watermark(session)
            max_id = await session.scalar(select(func.max(DomainEventLog.id)))
            exchange_max = await session.scalar(select(func.max(DomainEventLog.exchange_sequence)))
            robot_ids = await session.execute(select(User.id).where(User.role == "mm_bot"))
            await session.commit()
        self._watermark = int(watermark.materialized_seq)
        self._persisted_max_id = int(max_id or 0)
        self._robot_user_ids = {int(value) for value in robot_ids.scalars()}
        self._watermarks["materialized_seq"] = self._watermark
        self._watermarks["durable_seq"] = self._persisted_max_id
        self._watermarks["ingress_seq"] = int(exchange_max or 0)
        self._watermarks["matched_seq"] = 0  # Intent journal is not proof of matching.
        self._metrics["last_materialized_seq"] = self._watermark
        self._metrics["last_persisted_seq"] = self._persisted_max_id
        self._initialized = True

    async def materialize_catchup(self, timeout: float = 60.0) -> None:
        """Synchronously drain pending events into business tables.

        Called at startup before load-from-DB recovery, so the business tables
        (and therefore the rebuilt engine/mirrors) include every appended event.
        """
        await self.initialize()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.materialization_lag() > 0 and loop.time() < deadline:
            applied = await self._materialize_batch()
            if applied <= 0:
                break
        await self._retry_dead_letter(timeout=max(5.0, deadline - loop.time()))

    async def replay_command_state(self, exchange_core, *, since_seq: int = 0) -> int:
        """Rebuild ExchangeCore's quote generations from the durable journal.

        Only QUOTE_SET_REPLACE commands after ``since_seq`` are replayed; the
        latest snapshot already carries earlier generations/quote orders, so a
        full-journal scan on a large local database is unnecessary.
        """
        await self.initialize()
        replayed = 0
        async with self._session_factory() as session:
            rows = await session.execute(
                select(DomainEventLog)
                .where(DomainEventLog.command_type == "QUOTE_SET_REPLACE")
                .where(DomainEventLog.exchange_sequence > int(since_seq))
                .order_by(DomainEventLog.id.asc())
            )
            for row in rows.scalars():
                document = json.loads(str(row.payload_json))
                if not document.get("_command"):
                    continue
                payload = document.get("payload") if isinstance(document.get("payload"), dict) else {}
                operations = payload.get("operations") if isinstance(payload, dict) else []
                exchange_core.replay_quote_set(
                    {
                        "command_id": document.get("command_id") or row.command_id,
                        "exchange_sequence": document.get("exchange_sequence") or row.exchange_sequence,
                        "account_id": document.get("account_id") or row.account_id,
                        "symbol": document.get("symbol") or row.symbol,
                        "command_type": document.get("kind") or row.command_type,
                        "logical_timestamp": document.get("logical_timestamp") or row.logical_timestamp,
                        "config_version": document.get("config_version") or row.config_version,
                        "product_type": document.get("product_type") or row.product_type,
                        "strategy_instance": document.get("strategy_instance"),
                        "generation": document.get("generation"),
                        "payload": payload,
                        "operations": operations,
                    }
                )
                replayed += 1
        return replayed

    async def drain_append(self, timeout: float = 2.0) -> int:
        """Wait for queued AND already-dequeued SQL appends to finish."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self._queue.empty() and loop.time() < deadline:
            batch: list[dict] = []
            while len(batch) < self._batch_max:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if batch:
                job = self._schedule_append_batch(batch, queued=True)
                await asyncio.wait_for(asyncio.shield(job),
                                       timeout=max(0.0, deadline - loop.time()))
        await asyncio.wait_for(self._queue.join(), timeout=max(0.0, deadline - loop.time()))
        # Direct QuoteSet tails do not enter _queue, but an already admitted
        # batch must also finish before a drain can report an idle writer.
        if self._append_jobs:
            completed, pending = await asyncio.wait(tuple(self._append_jobs),
                                                    timeout=max(0.0, deadline - loop.time()))
            if pending:
                raise TimeoutError("durable execution append is still in flight")
            for job in completed:
                job.result()
        if self._blocked_task is not None or self._critical_blocked is not None:
            raise RuntimeError("durable execution writer is halted")
        return self._queue.qsize()

    async def _commit_append_batch(self, batch: list[dict], *, queued: bool = False):
        # Cancellation/timeout of one API waiter cannot abort a shared append.
        return await asyncio.shield(self._schedule_append_batch(batch, queued=queued))

    def _schedule_append_batch(self, batch: list[dict], *, queued: bool = False):
        # Register ownership synchronously after dequeue, before even a zero
        # timeout can cancel the caller. No dequeued batch may lose its owner.
        job = asyncio.create_task(self._commit_append_batch_owned(batch, queued=queued),
                                  name="persistence-append-batch")
        self._append_jobs.add(job)
        def finished(done):
            self._append_jobs.discard(done)
            if not done.cancelled():
                done.exception()
        job.add_done_callback(finished)
        return job

    async def _commit_append_batch_owned(self, batch: list[dict], *, queued: bool):
        try:
            async with self._append_lock:
                if self._blocked_task is not None or self._critical_blocked is not None:
                    raise RuntimeError("durable execution writer is halted")
                self._busy = True
                try:
                    return await self._append_batch(batch)
                finally:
                    self._busy = False
        except BaseException as exc:
            self._blocked_task = self._blocked_task or {**batch[-1], "error": str(exc)}
            self._stopping = True
            self._metrics["status"] = "HALTED"
            self._metrics["failed"] += len(batch)
            # Fail pending waiters explicitly; never use queue length or an
            # unrelated MAX(id) as evidence for their particular execution.
            for future in self._append_receipts.values():
                if not future.done():
                    future.set_exception(RuntimeError("durable execution append failed or was interrupted"))
            self._append_receipts.clear()
            raise
        finally:
            if queued:
                for _ in batch:
                    self._queue.task_done()

    async def flush(self, timeout: float = 30.0, *, materialize: bool = False) -> dict:
        """Drain the append queue; optionally wait for materialization catch-up."""
        await self.initialize()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if self._queue.empty() and not self._busy and not self._append_jobs and (
                not materialize or self.materialization_lag() <= 0
            ):
                break
            await asyncio.sleep(0.01)
        return self.metrics_snapshot()

    async def stop(self, timeout: float = 10.0) -> None:
        """Drain, materialize, then cancel background tasks."""
        self._stopping = True
        try:
            await self.flush(timeout=timeout, materialize=True)
        except Exception:
            logger.exception("persistence writer drain failed at stop")
        for task in (self._worker_task, self._materializer_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._worker_task, self._materializer_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    # ------------------------------------------------------------------
    # command journal and quote-set capture
    # ------------------------------------------------------------------
    def is_capturing(self) -> bool:
        return self._capture_var.get() is not None

    @contextmanager
    def capture_operations(self):
        """Capture per-level compatibility tasks without journaling them.

        QuoteSet uses the existing fast accounting code to keep its established
        settlement semantics, but commits exactly one command envelope.  The
        captured tasks are diagnostic only; replay uses the deterministic
        operation plan in the command envelope.
        """
        token = self._capture_var.set([])
        try:
            captured = self._capture_var.get()
            assert captured is not None
            yield captured
        finally:
            self._capture_var.reset(token)

    @contextmanager
    def ephemeral_quote_set_operations(self):
        """Mark safe robot quote animation without weakening normal bot writes."""
        token = self._ephemeral_quote_set_var.set(True)
        try:
            yield
        finally:
            self._ephemeral_quote_set_var.reset(token)

    async def append_command(self, record: dict) -> dict:
        """Append and commit one durable command before core execution."""
        await self.initialize()
        command_id = str(record.get("command_id") or "")
        if not command_id:
            raise ValueError("command_id is required")
        payload = dict(record.get("payload") or {})
        # Keep the operation plan inside the immutable journal payload.  A
        # replay after a crash does not need to rediscover random order ids.
        if record.get("operations") is not None:
            payload["operations"] = record.get("operations") or []
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        payload_digest = str(record.get("payload_hash") or sha256(serialized.encode("utf-8")).hexdigest())
        started = perf_counter()
        for attempt in range(12):
            try:
                async with self._session_factory() as session:
                    existing = await session.scalar(
                        select(DomainEventLog).where(DomainEventLog.command_id == command_id)
                    )
                    if existing is not None:
                        return {
                            "event_id": int(existing.id),
                            "durable_seq": int(existing.exchange_sequence or 0),
                            "exchange_sequence": int(existing.exchange_sequence or 0),
                            "command_id": command_id, "ack_stage": "JOURNALED",
                            "sequence_domain": "exchange_core_ingress",
                            "deduplicated": True,
                        }
                    now = datetime.now(tz=UTC)
                    row = DomainEventLog(
                        created_at=now,
                        product_type=str(record.get("product_type") or self._product_type({"kind": record.get("command_type")})),
                        symbol=str(record.get("symbol") or "unknown"),
                        user_id=int(record.get("account_id") or 0),
                        kind=str(record.get("command_type") or "COMMAND"),
                        payload_json=json.dumps(
                            {
                                "_command": True,
                                "command_id": command_id,
                                "exchange_sequence": int(record.get("exchange_sequence") or 0),
                                "account_id": int(record.get("account_id") or 0),
                                "user_id": int(record.get("account_id") or 0),
                                "symbol": str(record.get("symbol") or "unknown"),
                                "kind": str(record.get("command_type") or "COMMAND"),
                                "product_type": str(record.get("product_type") or "SPOT"),
                                "logical_timestamp": int(record.get("logical_timestamp") or 0),
                                "config_version": str(record.get("config_version") or "default"),
                                "strategy_instance": record.get("strategy_instance"),
                                "generation": record.get("generation"),
                                "payload": payload,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                        state="pending",
                        command_id=command_id,
                        exchange_sequence=int(record.get("exchange_sequence") or 0),
                        account_id=int(record.get("account_id") or 0),
                        command_type=str(record.get("command_type") or "COMMAND"),
                        logical_timestamp=int(record.get("logical_timestamp") or 0),
                        config_version=str(record.get("config_version") or "default"),
                        payload_hash=payload_digest,
                        durability_mode=settings.exchange_durability_mode,
                        sink_class="critical",
                    )
                    session.add(row)
                    await session.commit()
                    event_id = int(row.id)
                elapsed_ms = (perf_counter() - started) * 1000
                self._record_journal_commit(elapsed_ms)
                durable_seq = int(record.get("exchange_sequence") or event_id)
                self._persisted_max_id = max(self._persisted_max_id, event_id)
                self._watermarks["durable_seq"] = self._persisted_max_id
                self._watermarks["ingress_seq"] = max(self._watermarks["ingress_seq"], durable_seq)
                self._metrics["persisted"] += 1
                self._metrics["journal_commands"] += 1
                self._metrics["last_persisted_seq"] = self._persisted_max_id
                return {"event_id": event_id, "durable_seq": durable_seq, "deduplicated": False,
                        "exchange_sequence": int(record["exchange_sequence"]),
                        "command_id": command_id, "ack_stage": "JOURNALED",
                        "sequence_domain": "exchange_core_ingress"}
            except Exception as exc:
                if "database is locked" in str(exc).lower() and attempt < 11:
                    await asyncio.sleep(min(1.0, 0.05 * (attempt + 1)))
                    continue
                raise
        raise RuntimeError("journal append exhausted retries")

    def _record_journal_commit(self, elapsed_ms: float) -> None:
        self._metrics["last_journal_commit_ms"] = round(elapsed_ms, 3)
        self._journal_commit_samples.append(float(elapsed_ms))
        if len(self._journal_commit_samples) > 2000:
            self._journal_commit_samples = self._journal_commit_samples[-2000:]
        ordered = sorted(self._journal_commit_samples)
        index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.99) - 1))
        self._journal_commit_p99_ms = ordered[index] if ordered else 0.0
        self._metrics["journal_commit_p99_ms"] = round(self._journal_commit_p99_ms, 3)

    def mark_matched(self, sequence: int) -> None:
        self._watermarks["matched_seq"] = max(self._watermarks["matched_seq"], int(sequence))

    def mark_published(self, sequence: int) -> None:
        self._watermarks["published_seq"] = max(self._watermarks["published_seq"], int(sequence))

    def record_ephemeral_quote_set(self, operation_count: int) -> None:
        """Record a memory-mode robot QuoteSet without writing it to SQLite.

        Robot quote animation is intentionally restart-ephemeral in ``memory``
        mode.  Individual fast-path operations still pass through ``enqueue``:
        simple no-fill amendments are dropped, sparse resting-order lifecycle
        anchors stay durable, and every real fill remains durable.  Authorized
        FLOW preview traffic is handled before engine mutation and is counted
        separately as a display-only synthetic event.
        """
        self._metrics["ephemeral_quote_set_commands"] += 1
        self._metrics["ephemeral_quote_set_operations"] += max(0, int(operation_count))

    def durable_sequence(self) -> int:
        return int(self._watermarks["durable_seq"])

    def critical_sink_status(self) -> dict:
        return {
            "status": "HALTED" if self._critical_blocked else "HEALTHY",
            "blocked": dict(self._critical_blocked) if self._critical_blocked else None,
            "critical_materializer_seq": self._watermarks["critical_materialized_seq"],
        }

    def robot_user_ids_snapshot(self) -> set[int]:
        """Return the startup-loaded mm_bot registry as an immutable snapshot."""
        return set(self._robot_user_ids)

    def synthetic_flow_ready(self) -> bool:
        """Fail closed unless the synthetic pre-match route is safe to serve."""
        return (
            is_runtime_only_mode()
            and not settings.robot_flow_persistence_enabled
            and self._initialized
            and not self._stopping
            and self._blocked_task is None
            and self._critical_blocked is None
            and not self._queue.full()
        )

    def record_synthetic_flow(self, symbol: str, fill_count: int) -> None:
        """Observe display-only FLOW without pretending a durable event was dropped."""
        self._metrics["robot_flow_synthetic_events"] += 1
        self._metrics["robot_flow_synthetic_fills"] += max(0, int(fill_count))
        by_symbol = self._metrics["robot_flow_synthetic_by_symbol"]
        normalized = str(symbol or "unknown").upper()
        by_symbol[normalized] = int(by_symbol.get(normalized, 0)) + 1

    # ------------------------------------------------------------------
    # hot path (append-only event log)
    # ------------------------------------------------------------------
    def _robot_memory_task(self, task: dict) -> bool:
        user_id = int(task.get("user_id") or 0)
        return (
            (settings.persistence_mode == "memory" or platform_durable_contract())
            and (task.get("ephemeral_robot_quote") is True or user_id > 0 and user_id in self._robot_user_ids)
        )

    def is_ephemeral_quote_task(self, task: dict) -> bool:
        """The same no-write predicate used by enqueue; never drops real fills."""
        if str(task.get("kind") or "") not in {"spot_amend", "contract_amend"}:
            return False
        if not self._robot_memory_task(task):
            return False
        result = task.get("engine_result") if isinstance(task.get("engine_result"), dict) else {}
        return self._safe_ephemeral_quote_amend(task, result)

    def enqueue(self, task: dict) -> bool:
        captured = self._capture_var.get()
        if captured is not None:
            captured.append(dict(task))
            self._metrics["captured_operations"] += 1
            return True
        task = dict(task)
        kind = str(task.get("kind") or "")
        if kind in {"spot_place", "contract_place"} and self._robot_memory_task(task):
            # A previous restart-ephemeral quote with the same stable client id
            # may still exist in the durable view. Replay must replace it, not
            # report an idempotent success while leaving the engine level absent.
            task["allow_client_order_reuse"] = True
        if self.is_ephemeral_quote_task(task):
            # A simple no-fill requote changes only restart-ephemeral robot
            # animation. Real fills carry a maker pre-fill anchor that aligns
            # the durable order/reserve in the same replay transaction. Cancels,
            # crossing amendments and every fill-bearing task remain durable.
            self._metrics["robot_ephemeral_dropped"] = (
                self._metrics.get("robot_ephemeral_dropped", 0) + 1
            )
            return True
        self._seq += 1
        task["_seq"] = self._seq
        try:
            self._queue.put_nowait(task)
        except asyncio.QueueFull:
            return False
        self._metrics["enqueued"] += 1
        symbol = str(task.get("symbol") or "unknown")
        kind = str(task.get("kind") or "unknown")
        self._metrics["by_symbol"][symbol] = self._metrics["by_symbol"].get(symbol, 0) + 1
        self._metrics["by_kind"][kind] = self._metrics["by_kind"].get(kind, 0) + 1
        self._metrics["queue_size"] = self._queue.qsize()
        return True

    async def enqueue_durable(self, task: dict, *, timeout: float = 5.0) -> dict:
        """Append a critical execution task and wait for its SQL commit.

        Financial executions use this barrier after the actual result is known.
        Ordinary no-fill quote animation keeps its existing enqueue contract.
        """

        if self.is_capturing():
            raise RuntimeError("captured execution must use its outer durable batch")
        if self._blocked_task is not None or self._critical_blocked is not None or self._stopping:
            raise RuntimeError("durable execution writer is halted")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        previous = self._seq
        if not self.enqueue(task):
            await self.drain_append(timeout=max(0.0, deadline - loop.time()))
            previous = self._seq
            if not self.enqueue(task):
                raise RuntimeError("durable execution queue is full")
        if self._seq == previous:
            raise RuntimeError("ephemeral execution has no durable append receipt")
        receipt = loop.create_future()
        receipt.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self._append_receipts[self._seq] = receipt
        if self._worker_task is None or self._worker_task.done():
            await self.drain_append(timeout=max(0.0, deadline - loop.time()))
        if self._blocked_task is not None or self._critical_blocked is not None:
            raise RuntimeError("durable execution writer is halted")
        return await asyncio.wait_for(asyncio.shield(receipt), timeout=max(0.0, deadline - loop.time()))

    async def enqueue_durable_batch(self, tasks: list[dict], *, timeout: float = 5.0) -> dict:
        """Durably append a bounded QuoteSet execution tail in one SQL batch."""
        if self._blocked_task is not None or self._critical_blocked is not None or self._stopping:
            raise RuntimeError("durable execution writer is halted")
        batch: list[dict] = []
        for task in tasks:
            item = dict(task)
            self._seq += 1
            item["_seq"] = self._seq
            batch.append(item)
            self._metrics["enqueued"] += 1
            symbol = str(item.get("symbol") or "unknown")
            kind = str(item.get("kind") or "unknown")
            self._metrics["by_symbol"][symbol] = self._metrics["by_symbol"].get(symbol, 0) + 1
            self._metrics["by_kind"][kind] = self._metrics["by_kind"].get(kind, 0) + 1
        if batch:
            receipt = await asyncio.wait_for(asyncio.shield(self._schedule_append_batch(batch)), timeout=timeout)
        else:
            receipt = {"event_id": int(self._persisted_max_id), "durable_seq": int(self._watermarks["durable_seq"])}
        if self._blocked_task is not None or self._critical_blocked is not None:
            raise RuntimeError("durable execution writer is halted")
        return {
            "accepted": len(batch),
            **receipt,
        }

    @staticmethod
    def _safe_ephemeral_quote_amend(task: dict, engine_result: dict) -> bool:
        """Drop only non-crossing, no-fill robot quote animation."""
        if str(task.get("amend_kind") or "simple") != "simple" or engine_result.get("fills"):
            return False
        return task.get("new_remaining") is not None

    async def _run(self) -> None:
        try:
            while not self._stopping or not self._queue.empty():
                try:
                    first = await asyncio.wait_for(
                        self._queue.get(), timeout=self._batch_window_seconds
                    )
                except asyncio.TimeoutError:
                    continue
                batch = [first]
                while len(batch) < self._batch_max:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                try:
                    await self._commit_append_batch(batch, queued=True)
                except Exception:
                    # The batch owner already recorded the fail-closed state.
                    break
                self._metrics["queue_size"] = self._queue.qsize()
                if self._stopping:
                    break
        finally:
            pass

    async def _append_batch(self, batch: list[dict]) -> dict:
        last_exc: Exception | None = None
        for attempt in range(12):
            committed = False
            try:
                async with self._session_factory() as session:
                    now = datetime.now(tz=UTC)
                    rows = []
                    for task in batch:
                        row = DomainEventLog(
                            created_at=now,
                            product_type=self._product_type(task),
                            symbol=str(task["symbol"]),
                            user_id=int(task["user_id"]),
                            kind=str(task["kind"]),
                            payload_json=json.dumps(task, ensure_ascii=False, default=str),
                            state="pending",
                            command_type=str(task.get("kind") or "legacy"),
                            sink_class="critical" if self._is_critical_task(task) else "non-critical",
                        )
                        session.add(row)
                        rows.append(row)
                    await session.flush()
                    event_ids = [int(row.id) for row in rows]
                    await session.commit()
                    committed = True
                self._persisted_max_id = max(self._persisted_max_id, max(event_ids))
                self._watermarks["durable_seq"] = self._persisted_max_id
                self._metrics["persisted"] += len(batch)
                self._metrics["last_persisted_seq"] = self._persisted_max_id
                self._metrics["batch_count"] += 1
                for task, event_id in zip(batch, event_ids):
                    future = self._append_receipts.pop(int(task["_seq"]), None)
                    if future is not None and not future.done():
                        future.set_result({"event_id": event_id, "durable_seq": event_id})
                return {"event_id": event_ids[-1], "durable_seq": event_ids[-1]}
            except Exception as exc:
                last_exc = exc
                if not committed and "database is locked" in str(exc).lower():
                    await asyncio.sleep(min(1.0, 0.05 * (attempt + 1)))
                    continue
                break
        logger.critical(
            "domain event append failed after retries count=%s: %s; writer halted fail-closed",
            len(batch),
            last_exc,
        )
        raise RuntimeError("domain event append failed after retries") from last_exc

    @staticmethod
    def _product_type(task: dict) -> str:
        kind = str(task.get("kind") or "")
        return "PERP" if kind.startswith("contract_") else "SPOT"

    @staticmethod
    def _is_critical_task(task: dict) -> bool:
        kind = str(task.get("kind") or task.get("command_type") or "")
        if kind == "quote_set_replace" or kind.upper() == "QUOTE_SET_REPLACE":
            return True
        return kind.startswith(("spot_", "contract_", "trade", "balance", "ledger", "funding", "liquidation", "adl"))

    def _batch_contains_critical(self, events: list[dict]) -> bool:
        return any(self._is_critical_task(json.loads(event["payload_json"])) for event in events)

    async def _block_critical_batch(self, events: list[dict], exc: Exception) -> None:
        first_id = int(events[0]["id"]) if events else None
        message = str(exc)[:2000]
        self._critical_blocked = {
            "event_id": first_id,
            "kind": events[0].get("kind") if events else None,
            "error": message,
            "blocked_at": datetime.now(tz=UTC).isoformat(),
        }
        self._blocked_task = {
            "event_id": first_id,
            "kind": events[0].get("kind") if events else None,
            "error": message,
        }
        self._metrics["critical_sink_failures"] += 1
        self._metrics["status"] = "HALTED"
        # Error annotation is allowed by the append-only trigger.  The event
        # remains pending and the watermark deliberately does not move.
        if first_id is not None:
            try:
                async with self._session_factory() as session:
                    row = await session.get(DomainEventLog, first_id)
                    if row is not None:
                        row.error = message
                    await session.commit()
            except Exception:
                logger.exception("failed to annotate critical sink block event=%s", first_id)

    # ------------------------------------------------------------------
    # background materialization
    # ------------------------------------------------------------------
    async def _materialize_loop(self) -> None:
        cleanup_counter = 0
        try:
            while True:
                try:
                    if self._stopping and self.materialization_lag() <= 0:
                        break
                    await self._materialize_batch()
                    cleanup_counter += 1
                    if cleanup_counter >= 200 and self._retention_seconds > 0:
                        cleanup_counter = 0
                        await self._retention_cleanup()
                except Exception:
                    logger.exception("persistence materializer loop error")
                await asyncio.sleep(self._materialize_interval_seconds)
        finally:
            pass

    async def _ensure_watermark(self, session: AsyncSession) -> DomainEventWatermark:
        watermark = await session.scalar(
            select(DomainEventWatermark).where(DomainEventWatermark.id == 1)
        )
        if watermark is None:
            watermark = DomainEventWatermark(
                id=1, materialized_seq=0, updated_at=datetime.now(tz=UTC)
            )
            session.add(watermark)
            await session.flush()
        return watermark

    async def _materialize_batch(self, *, busy_attempt: int = 0) -> int:
        events: list[dict] = []
        try:
            async with self._session_factory() as session:
                watermark = await self._ensure_watermark(session)
                rows = await session.execute(
                    select(DomainEventLog)
                    .where(DomainEventLog.id > int(watermark.materialized_seq))
                    .order_by(DomainEventLog.id.asc())
                    .limit(self._materialize_batch_max)
                )
                loaded = list(rows.scalars().all())
                if not loaded:
                    self._watermark = int(watermark.materialized_seq)
                    return 0
                read_last_id = int(loaded[-1].id)
                # Detach from the session immediately: every downstream step
                # works on plain data so a failed batch can be retried without
                # ORM DetachedInstanceError and without double-applying rows.
                events = [
                    {
                        "id": int(event.id),
                        "kind": str(event.kind),
                        "payload_json": str(event.payload_json),
                        "exchange_sequence": int(event.exchange_sequence or event.id),
                        "sink_class": str(event.sink_class or "critical"),
                    }
                    for event in loaded
                ]
                events, dropped_ids = self._coalesce_events(events)
                markets: dict[str, Market] = {}
                users: dict[int, User] = {}
                for event in events:
                    task = json.loads(event["payload_json"])
                    symbol = str(task["symbol"])
                    if symbol not in markets:
                        market = await session.scalar(select(Market).where(Market.symbol == symbol))
                        if market is not None:
                            markets[symbol] = market
                    user_id = int(task["user_id"])
                    if user_id not in users:
                        user = await session.scalar(select(User).where(User.id == user_id))
                        if user is not None:
                            users[user_id] = user
                for event in events:
                    task = json.loads(event["payload_json"])
                    task["_seq"] = event["id"]
                    await self._apply_task(
                        session,
                        task,
                        market=markets.get(str(task["symbol"])),
                        user=users.get(int(task["user_id"])),
                    )
                now = datetime.now(tz=UTC)
                for event in events:
                    row = await session.get(DomainEventLog, event["id"])
                    if row is not None:
                        row.state = "applied"
                        row.applied_at = now
                        row.error = None
                if dropped_ids:
                    for event_id in dropped_ids:
                        row = await session.get(DomainEventLog, event_id)
                        if row is not None:
                            row.state = "applied"
                            row.applied_at = now
                            row.error = None
                watermark.materialized_seq = read_last_id
                watermark.updated_at = now
                await session.commit()
        except Exception as exc:
            lowered = str(exc).lower()
            sqlite_busy = "database is locked" in lowered or "database table is locked" in lowered
            if sqlite_busy and busy_attempt < 6:
                base = max(float(settings.exchange_materializer_critical_retry_seconds), 0.01)
                await asyncio.sleep(min(2.0, base * (2**busy_attempt)))
                return await self._materialize_batch(busy_attempt=busy_attempt + 1)
            if await self._batch_already_converged(events, exc):
                # SQLite busy 重试窗口内，同一个 place 事件可能被两个批次
                # 先后重放：先到的批次已把订单行/成交/账本原子提交，后到的
                # 批次 INSERT 命中 UNIQUE(order_id)。此时耐用状态已经收敛，
                # 幂等跳过并推进 watermark，而不是 fail-closed HALT。
                await self._skip_converged_events(events, exc)
                return 0
            if self._batch_contains_critical(events):
                if all(self._is_skippable_paper_quote_event(event) for event in events):
                    # Paper 报价（paperq-*）是重启重建的临时态。其 write-behind
                    # 重放任务在重启后重放历史报价时，会与 maker 的持久化保证金
                    # 状态不一致（余额变负等），此时跳过整批报价事件继续推进，
                    # 而不是 fail-closed HALT 冻结所有后续物化。用户订单事件不含
                    # 在内，仍保持严格的 HALT 保护。
                    await self._skip_paper_quote_events(events, exc)
                    return 0
                await self._block_critical_batch(events, exc)
                return 0
            await self._materialize_individually(events)
            return len(events)
        self._watermark = read_last_id
        self._metrics["materialized"] += len(events)
        for event in events:
            task = json.loads(event["payload_json"])
            symbol = str(task.get("symbol") or "unknown")
            self._metrics["applied_by_symbol"][symbol] = (
                self._metrics["applied_by_symbol"].get(symbol, 0) + 1
            )
        self._metrics["last_materialized_seq"] = self._watermark
        self._metrics["last_materialized_at"] = datetime.now(tz=UTC).isoformat()
        self._metrics["materialize_batch_count"] += 1
        max_event_id = read_last_id  # All materialized rows share the DomainEventLog.id clock.
        self._watermarks["materialized_seq"] = max(self._watermarks["materialized_seq"], max_event_id)
        self._watermarks["critical_materialized_seq"] = max(
            self._watermarks["critical_materialized_seq"], max_event_id
        )
        self._critical_blocked = None
        if self._blocked_task is not None and self._blocked_task.get("event_id") in {
            event.get("id") for event in events
        }:
            self._blocked_task = None
        self._metrics["status"] = "HEALTHY"
        return len(events)

    @staticmethod
    def _is_skippable_paper_quote_event(event: dict) -> bool:
        """Paper quote order events may be skipped when replay fails.

        Only order events from restart-ephemeral quote ladders qualify:
        ``paperq-`` (Paper built-in market maker), ``perpmm-`` (PERP_MM
        strategy ladder) and ``mmv2-`` (Lite strategy ladder).  Quote ladders
        are restart-ephemeral; skipping their replay only loses maker-side
        durable bookkeeping that the next restart re-provisions.  FLOW IOC
        (``flowv2-``/``perpmm-flow-``) and user order events never match this
        filter and keep the fail-closed HALT protection.
        """
        try:
            task = json.loads(event.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        kind = str(task.get("kind") or event.get("kind") or "")
        if kind not in {
            "spot_place",
            "spot_amend",
            "spot_cancel",
            "contract_place",
            "contract_amend",
            "contract_cancel",
        }:
            return False
        payload = task.get("payload") or {}
        client_order_id = str(payload.get("client_order_id") or task.get("client_order_id") or "")
        if "-flow-" in client_order_id or client_order_id.startswith("flow"):
            # FLOW IOC 是真实低频成交，必须按 PRD 口径持久化，永不跳过。
            return False
        if client_order_id.startswith(("paperq-", "perpmm-", "mmv2-")):
            return True
        order_id = str(task.get("order_id") or "")
        return order_id.startswith("qord_")

    async def _skip_paper_quote_events(self, events: list[dict], exc: Exception) -> None:
        """Mark one batch of failed paper-quote events as skipped and advance."""
        try:
            async with self._session_factory() as session:
                now = datetime.now(tz=UTC)
                for event in events:
                    row = await session.get(DomainEventLog, event["id"])
                    if row is not None:
                        row.state = "applied"
                        row.applied_at = now
                        row.error = f"skipped: paper quote replay ({str(exc)[:120]})"
                watermark = await self._ensure_watermark(session)
                read_last_id = int(events[-1]["id"])
                watermark.materialized_seq = read_last_id
                watermark.updated_at = now
                await session.commit()
            self._watermark = read_last_id
            self._metrics["paper_quote_events_skipped"] = (
                int(self._metrics.get("paper_quote_events_skipped", 0)) + len(events)
            )
            self._critical_blocked = None
            self._blocked_task = None
            self._metrics["status"] = "HEALTHY"
            logger.warning(
                "paper quote replay skipped count=%s reason=%s",
                len(events),
                str(exc)[:200],
            )
        except Exception:
            logger.exception("failed to skip paper quote events")

    async def _batch_already_converged(self, events: list[dict], exc: Exception) -> bool:
        """True when every event in the batch is a place whose durable row exists.

        A retried materializer batch can replay a fill-bearing place event that
        an earlier batch already committed atomically (order row + fills +
        ledger).  The second INSERT then fails with UNIQUE(orders.order_id).
        Replaying again would be redundant; verify the rows really exist before
        idempotently skipping so unrelated IntegrityErrors still fail closed.
        """
        text = "".join(str(exc).lower().split())
        if "uniqueconstraintfailed:orders.order_id" not in text:
            return False
        place_tasks: list[dict] = []
        for event in events:
            try:
                task = json.loads(event["payload_json"])
            except (TypeError, json.JSONDecodeError):
                return False
            if str(task.get("kind") or "") not in {"spot_place", "contract_place"}:
                return False
            place_tasks.append(task)
        if not place_tasks:
            return False
        try:
            async with self._session_factory() as session:
                for task in place_tasks:
                    row = await session.scalar(
                        select(Order).where(Order.order_id == str(task.get("order_id") or ""))
                    )
                    if row is None:
                        return False
                    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
                    if str(payload.get("client_order_id") or "") and str(row.client_order_id or "") != str(payload.get("client_order_id")):
                        return False
            return True
        except Exception:
            return False

    async def _skip_converged_events(self, events: list[dict], exc: Exception) -> None:
        """Mark one batch of already-converged place replays as applied."""
        try:
            async with self._session_factory() as session:
                now = datetime.now(tz=UTC)
                read_last_id = int(events[-1]["id"])
                for event in events:
                    row = await session.get(DomainEventLog, event["id"])
                    if row is not None:
                        row.state = "applied"
                        row.applied_at = now
                        row.error = f"skipped: replay already converged ({str(exc)[:120]})"
                watermark = await self._ensure_watermark(session)
                watermark.materialized_seq = read_last_id
                watermark.updated_at = now
                await session.commit()
            self._watermark = read_last_id
            self._metrics["converged_replay_skips"] = (
                int(self._metrics.get("converged_replay_skips", 0)) + len(events)
            )
            self._metrics["status"] = "HEALTHY"
            logger.info(
                "materializer skipped converged place replay count=%s reason=%s",
                len(events),
                str(exc)[:160],
            )
        except Exception:
            logger.exception("failed to skip converged place replay events")

    async def _materialize_individually(self, events: list[dict]) -> None:
        for event in events:
            task = json.loads(event["payload_json"])
            task["_seq"] = event["id"]
            last_exc: Exception | None = None
            for attempt in range(12):
                try:
                    async with self._session_factory() as session:
                        market = await session.scalar(
                            select(Market).where(Market.symbol == str(task["symbol"]))
                        )
                        user = await session.scalar(
                            select(User).where(User.id == int(task["user_id"]))
                        )
                        await self._apply_task(session, task, market=market, user=user)
                        row = await session.get(DomainEventLog, event["id"])
                        if row is not None:
                            row.state = "applied"
                            row.applied_at = datetime.now(tz=UTC)
                        watermark = await self._ensure_watermark(session)
                        watermark.materialized_seq = event["id"]
                        watermark.updated_at = datetime.now(tz=UTC)
                        await session.commit()
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    if "database is locked" in str(exc).lower():
                        await asyncio.sleep(min(1.0, 0.05 * (attempt + 1)))
                        continue
                    if isinstance(exc, (ContractValidationError, OrderValidationError)):
                        # Deterministic business-state race (the in-memory state
                        # already converged); quarantine without blocking later
                        # events and without halting the hot path.
                        break
            if last_exc is None:
                self._metrics["materialized"] += 1
                symbol = str(task.get("symbol") or "unknown")
                self._metrics["applied_by_symbol"][symbol] = (
                    self._metrics["applied_by_symbol"].get(symbol, 0) + 1
                )
                continue
            # Quarantine this event and advance the watermark past it.
            async with self._session_factory() as session:
                row = await session.get(DomainEventLog, event["id"])
                if row is not None:
                    row.state = "failed"
                    row.error = str(last_exc)[:2000]
                watermark = await self._ensure_watermark(session)
                watermark.materialized_seq = event["id"]
                watermark.updated_at = datetime.now(tz=UTC)
                await session.commit()
            if isinstance(last_exc, (ContractValidationError, OrderValidationError)):
                logger.warning(
                    "domain event materialization skipped (business-state race) id=%s kind=%s: %s",
                    event["id"],
                    event["kind"],
                    last_exc,
                )
                self._metrics["skipped"] += 1
            else:
                logger.critical(
                    "domain event materialization dead-lettered id=%s kind=%s: %s",
                    event["id"],
                    event["kind"],
                    last_exc,
                )
                self._metrics["dead_letter"] += 1
            self._dead_letter.append(dict(task))
        if events:
            self._watermark = int(events[-1]["id"])
            self._metrics["last_materialized_seq"] = self._watermark
            self._metrics["last_materialized_at"] = datetime.now(tz=UTC).isoformat()

    def _coalesce_events(self, events: list[dict]) -> tuple[list[dict], set[int]]:
        """Drop intermediate events superseded by a later event in the batch.

        A later simple amend makes earlier amends of the same order redundant;
        a no-fill place followed by a cancel makes the place redundant.  This is
        the materializer's write-coalescing, matching event-sourcing sinks.
        """
        if len(events) < 2:
            return events, set()
        last_amend: dict[str, int] = {}
        place_idx: dict[str, int] = {}
        cancel_idx: dict[str, int] = {}
        drop: set[int] = set()
        tasks = [json.loads(event["payload_json"]) for event in events]

        def has_fill_dependency(order_id: str, start: int, end: int) -> bool:
            for task in tasks[start:end]:
                fills = (task.get("engine_result") or {}).get("fills") or []
                if not fills:
                    continue
                if str(task.get("order_id") or "") == order_id:
                    return True
                if any(
                    isinstance(fill, dict)
                    and str(fill.get("maker_order_id") or "") == order_id
                    for fill in fills
                ):
                    return True
            return False

        for i, event in enumerate(events):
            task = tasks[i]
            order_id = str(task.get("order_id") or "")
            if not order_id:
                continue
            if event["kind"] in {"spot_amend", "contract_amend"} and task.get("amend_kind") in (
                None,
                "simple",
            ):
                last_amend[order_id] = i
            elif event["kind"] in {"spot_place", "contract_place"}:
                fills = (task.get("engine_result") or {}).get("fills") or []
                if not fills:
                    place_idx[order_id] = i
            elif event["kind"] in {"spot_cancel", "contract_cancel"}:
                cancel_idx[order_id] = i
        for order_id, idx in last_amend.items():
            for i in range(idx):
                event = events[i]
                if event["kind"] in {"spot_amend", "contract_amend"}:
                    task = tasks[i]
                    if str(task.get("order_id") or "") == order_id:
                        if has_fill_dependency(order_id, i + 1, idx):
                            continue
                        drop.add(i)
        for order_id, cancel_i in cancel_idx.items():
            place_i = place_idx.get(order_id)
            if place_i is None or place_i >= cancel_i:
                continue
            if has_fill_dependency(order_id, place_i + 1, cancel_i):
                # A real fill causally depends on this maker existing. Never
                # coalesce its place/amend chain across the settlement event.
                continue
            drop.add(place_i)
            # A place followed by amend(s) then cancel: the amends are moot too.
            for i in range(place_i + 1, cancel_i):
                event = events[i]
                if event["kind"] in {"spot_amend", "contract_amend"}:
                    task = tasks[i]
                    if str(task.get("order_id") or "") == order_id:
                        drop.add(i)
        if drop:
            self._metrics["coalesced"] += len(drop)
            return (
                [event for i, event in enumerate(events) if i not in drop],
                {events[i]["id"] for i in drop},
            )
        return events, set()

    async def _retention_cleanup(self) -> None:
        if self._retention_seconds <= 0:
            return
        cutoff = datetime.now(tz=UTC) - timedelta(seconds=self._retention_seconds)
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    delete(DomainEventLog).where(
                        DomainEventLog.state == "applied",
                        DomainEventLog.applied_at < cutoff,
                    )
                )
                # Superseded/orphaned events dropped by coalescing stay
                # 'pending' below the watermark and are never re-read; remove
                # them so the append-only log does not accumulate dead rows.
                orphan_result = await session.execute(
                    delete(DomainEventLog).where(
                        DomainEventLog.state == "pending",
                        DomainEventLog.id <= self._watermark,
                    )
                )
                await session.commit()
            if result.rowcount:
                self._metrics["retention_removed"] += int(result.rowcount)
            if orphan_result.rowcount:
                self._metrics["retention_removed"] += int(orphan_result.rowcount)
        except Exception:
            logger.exception("domain event log retention cleanup failed")

    async def _retry_dead_letter(self, timeout: float = 5.0) -> None:
        """Startup repair pass: re-attempt previously quarantined events once."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            async with self._session_factory() as session:
                rows = await session.execute(
                    select(DomainEventLog)
                    .where(DomainEventLog.state == "failed")
                    .order_by(DomainEventLog.id.asc())
                    .limit(64)
                )
                failed = [
                    {
                        "id": int(event.id),
                        "kind": str(event.kind),
                        "payload_json": str(event.payload_json),
                    }
                    for event in rows.scalars().all()
                ]
            if not failed:
                return
            for event in failed:
                task = json.loads(event["payload_json"])
                task["_seq"] = event["id"]
                try:
                    async with self._session_factory() as session:
                        market = await session.scalar(
                            select(Market).where(Market.symbol == str(task["symbol"]))
                        )
                        user = await session.scalar(
                            select(User).where(User.id == int(task["user_id"]))
                        )
                        await self._apply_task(session, task, market=market, user=user)
                        row = await session.get(DomainEventLog, event["id"])
                        if row is not None:
                            row.state = "applied"
                            row.applied_at = datetime.now(tz=UTC)
                            row.error = None
                        await session.commit()
                    self._metrics["materialized"] += 1
                except Exception as exc:
                    logger.warning(
                        "dead-letter retry still failing id=%s kind=%s: %s",
                        event["id"],
                        event["kind"],
                        exc,
                    )

    # ------------------------------------------------------------------
    # replay dispatch (shared with materializer)
    # ------------------------------------------------------------------
    async def _apply_task(
        self,
        session: AsyncSession,
        task: dict,
        *,
        market: Market | None = None,
        user: User | None = None,
    ) -> None:
        kind = str(task["kind"])
        symbol = str(task["symbol"])
        if kind.upper() == "QUOTE_SET_REPLACE":
            command_payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            operations = command_payload.get("operations") if isinstance(command_payload, dict) else []
            if not isinstance(operations, list):
                raise RuntimeError("quote set journal operations are missing")
            # QuoteSet is a whole-book replacement: a place reuses the old
            # level's client_order_id, so any still-live order with the same
            # client id must be cancelled first.  Otherwise the replay skips the
            # insert (live-id conflict), the DB misses the new order and the
            # periodic reconcile prunes it from the engine.
            replace_client_ids: list[tuple[str, str]] = []
            for operation in operations:
                if not isinstance(operation, dict):
                    continue
                payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
                if str(operation.get("action") or "") == "place" and payload.get("client_order_id"):
                    replace_client_ids.append(
                        (str(operation.get("order_id") or ""), str(payload["client_order_id"]))
                    )
            for order_id, client_id in replace_client_ids:
                stale = await session.scalar(
                    select(Order).where(
                        Order.market_id == market.id,
                        Order.product_type == str(task.get("product_type") or "SPOT"),
                        Order.client_order_id == client_id,
                        Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                    )
                )
                if stale is not None and str(stale.order_id) != order_id:
                    service = (
                        self._contract_service
                        if str(task.get("product_type") or "SPOT") == "PERP"
                        else self._order_service
                    )
                    if service is not None:
                        try:
                            await service.cancel_order_replay(
                                session,
                                user,
                                market,
                                stale.order_id,
                                {"now": task.get("now") or datetime.now(tz=UTC).isoformat()},
                            )
                        except Exception:
                            logger.warning(
                                "quote set replay stale-client cancel failed order_id=%s client=%s",
                                stale.order_id,
                                client_id,
                            )
            for operation in operations:
                if not isinstance(operation, dict) or operation.get("action") == "noop":
                    continue
                child = dict(operation.get("task") or {})
                action = str(operation.get("action") or "")
                if not child:
                    child = {
                        "kind": (
                            "contract_place" if task.get("product_type") == "PERP" and action == "place" else
                            "contract_amend" if task.get("product_type") == "PERP" and action == "amend" else
                            "contract_cancel" if task.get("product_type") == "PERP" and action == "cancel" else
                            "spot_place" if action == "place" else
                            "spot_amend" if action == "amend" else
                            "spot_cancel"
                        ),
                        "symbol": symbol,
                        "user_id": int(task.get("account_id") or task.get("user_id") or 0),
                        "order_id": operation.get("order_id"),
                        "payload": operation.get("payload") or {},
                        "sequence_number": operation.get("sequence_number"),
                        "now": operation.get("now") or "",
                        "engine_result": operation.get("engine_result") or {
                            "remaining_quantity": str((operation.get("desired") or {}).get("quantity") or "0"),
                            "placed_on_book": action == "place",
                            "fills": [],
                        },
                        "reserve": operation.get("reserve") or {"asset": "USDT", "amount": "0"},
                        "leverage": (operation.get("reserve") or {}).get("leverage"),
                        "new_remaining": operation.get("new_remaining") or (operation.get("desired") or {}).get("quantity"),
                        "amend_kind": "simple",
                        "side": operation.get("side") or (operation.get("desired") or {}).get("side"),
                    }
                child["_seq"] = task.get("_seq")
                if not child.get("now"):
                    raise RuntimeError("quote set operation timestamp is missing")
                await self._apply_task(session, child, market=market, user=user)
            return
        if market is None:
            market = await session.scalar(select(Market).where(Market.symbol == symbol))
        if market is None:
            raise RuntimeError(f"market not found: {symbol}")
        if user is None:
            user = await session.scalar(select(User).where(User.id == int(task["user_id"])))
        if user is None:
            raise RuntimeError(f"user not found: {task['user_id']}")
        if kind in {"spot_place", "contract_place"}:
            existing_order = await session.scalar(
                select(Order).where(Order.order_id == str(task["order_id"]))
            )
            if existing_order is not None:
                logger.info(
                    "replay duplicate order_id, skipping kind=%s order_id=%s",
                    kind,
                    task.get("order_id"),
                )
                return
            client_id = (task.get("payload") or {}).get("client_order_id")
            if client_id:
                existing = await session.scalar(
                    select(Order).where(
                        Order.user_id == user.id,
                        Order.market_id == market.id,
                        Order.product_type == market.product_type,
                        Order.client_order_id == client_id,
                        Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                    )
                )
                if existing is not None:
                    if bool(task.get("allow_client_order_reuse")):
                        service = self._contract_service if kind == "contract_place" else self._order_service
                        if service is None:
                            raise RuntimeError(f"order service unavailable for reusable client id: {client_id}")
                        await service.cancel_order_replay(
                            session,
                            user,
                            market,
                            str(existing.order_id),
                            {
                                "now": task.get("now") or datetime.now(tz=UTC).isoformat(),
                                "side": str(existing.side),
                                "changes": [],
                            },
                        )
                    else:
                        logger.info(
                            "replay duplicate live client_order_id, skipping kind=%s order_id=%s client=%s",
                            kind,
                            task.get("order_id"),
                            client_id,
                        )
                        return
        if kind in {"spot_cancel", "contract_cancel", "spot_amend", "contract_amend"}:
            existing = await session.scalar(select(Order).where(Order.order_id == str(task["order_id"])))
            if existing is None:
                logger.info(
                    "replay target already absent from DB, skipping kind=%s order_id=%s",
                    kind,
                    task.get("order_id"),
                )
                return
        engine_result = task.get("engine_result")
        if isinstance(engine_result, dict):
            fills = [
                fill
                for fill in engine_result.get("fills", [])
                if isinstance(fill, dict) and fill.get("maker_order_id")
            ]
            maker_ids = {
                str(fill.get("maker_order_id"))
                for fill in fills
            }
            if maker_ids:
                maker_rows = (
                    await session.execute(
                        select(Order.order_id, Order.user_id).where(Order.order_id.in_(maker_ids))
                    )
                ).all()
                maker_users = {str(order_id): int(user_id) for order_id, user_id in maker_rows}
                missing = maker_ids - set(maker_users)
                if missing:
                    if int(task.get("maker_anchor_version") or 0) >= 1:
                        raise RuntimeError(
                            f"replay depends on unpersisted makers kind={kind} missing={sorted(missing)[:5]}"
                        )
                    logger.info(
                        "replay depends on unpersisted makers, skipping kind=%s order_id=%s missing=%s",
                        kind,
                        task.get("order_id"),
                        sorted(missing)[:5],
                    )
                    return
                if int(task.get("maker_anchor_version") or 0) >= 1:
                    raw_anchors = task.get("maker_pre_fill_anchors")
                    if not isinstance(raw_anchors, list):
                        raise RuntimeError(f"maker pre-fill anchors are missing kind={kind}")
                    anchors = {
                        str(anchor.get("order_id")): anchor
                        for anchor in raw_anchors
                        if isinstance(anchor, dict) and anchor.get("order_id")
                    }
                    if maker_ids - set(anchors):
                        raise RuntimeError(
                            f"maker pre-fill anchor coverage missing kind={kind} "
                            f"makers={sorted(maker_ids - set(anchors))[:5]}"
                        )
                    fill_qty_by_maker: dict[str, Decimal] = {}
                    for fill in fills:
                        maker_id = str(fill["maker_order_id"])
                        anchor = anchors[maker_id]
                        if int(fill.get("maker_user_id") or 0) != int(anchor.get("user_id") or 0):
                            raise RuntimeError(f"maker fill user does not match pre-fill anchor: {maker_id}")
                        fill_price = Decimal(str(fill.get("price") or "0"))
                        anchor_price = Decimal(str(anchor.get("price") or "0"))
                        # SQLite/JSON round-trips can retain a sub-tick binary
                        # tail in a QuoteSet anchor (for example
                        # 3401.360000000000127329) while the matcher emits the
                        # market-normalized 3401.36.  Reject real price drift,
                        # but do not halt the Paper writer on representation
                        # dust below one satoshi/price tick precision.
                        if abs(fill_price - anchor_price) > Decimal("1e-8"):
                            raise RuntimeError(f"maker fill price does not match pre-fill anchor: {maker_id}")
                        fill_qty_by_maker[maker_id] = fill_qty_by_maker.get(maker_id, Decimal("0")) + Decimal(
                            str(fill.get("quantity") or "0")
                        )
                    replay_now = datetime.fromisoformat(str(task["now"]))
                    for maker_id in sorted(maker_ids):
                        anchor = anchors[maker_id]
                        if int(anchor.get("user_id") or 0) != maker_users[maker_id]:
                            raise RuntimeError(f"maker pre-fill anchor user mismatch: {maker_id}")
                        if fill_qty_by_maker[maker_id] > Decimal(
                            str(anchor.get("remaining_quantity") or "0")
                        ):
                            raise RuntimeError(f"maker fill exceeds pre-fill anchor: {maker_id}")
                        if maker_users[maker_id] not in self._robot_user_ids:
                            continue
                        if kind.startswith("contract_"):
                            if self._contract_service is None:
                                raise RuntimeError("contract service is not configured for maker anchor replay")
                            await self._contract_service.align_robot_maker_pre_fill_replay(
                                session,
                                market,
                                anchor,
                                fill_quantity=fill_qty_by_maker[maker_id],
                                now=replay_now,
                                minimum_sequence=int(task.get("sequence_number") or 0),
                            )
                        else:
                            await self._order_service.align_robot_maker_pre_fill_replay(
                                session,
                                market,
                                anchor,
                                fill_quantity=fill_qty_by_maker[maker_id],
                                now=replay_now,
                                minimum_sequence=int(task.get("sequence_number") or 0),
                            )
                        self._metrics["maker_anchor_alignments"] += 1
        if kind == "spot_place":
            payload = OrderCreateRequest.model_validate(task["payload"])
            await self._order_service.place_order_replay(session, user, market, payload, task)
        elif kind == "spot_cancel":
            await self._order_service.cancel_order_replay(
                session, user, market, str(task["order_id"]), task
            )
        elif kind == "spot_amend":
            payload = OrderAmendRequest.model_validate(task["payload"])
            await self._order_service.amend_order_replay(
                session, user, market, str(task["order_id"]), payload, task
            )
        elif kind == "contract_place":
            if self._contract_service is None:
                raise RuntimeError("contract service is not configured for persistence writer")
            payload = ContractOrderCreateRequest.model_validate(task["payload"])
            await self._contract_service.place_order_replay(session, user, market, payload, task)
        elif kind == "contract_cancel":
            if self._contract_service is None:
                raise RuntimeError("contract service is not configured for persistence writer")
            await self._contract_service.cancel_order_replay(
                session, user, market, str(task["order_id"]), task
            )
        elif kind == "contract_amend":
            if self._contract_service is None:
                raise RuntimeError("contract service is not configured for persistence writer")
            payload = ContractOrderAmendRequest.model_validate(task["payload"])
            await self._contract_service.amend_order_replay(
                session, user, market, str(task["order_id"]), payload, task
            )
        else:
            raise RuntimeError(f"unsupported persistence task kind: {kind}")

    # ------------------------------------------------------------------
    # metrics
    # ------------------------------------------------------------------
    def materialization_lag(self) -> int:
        return max(0, int(self._persisted_max_id - self._watermark))

    def metrics_snapshot(self) -> dict:
        snapshot = dict(self._metrics)
        snapshot["persistence_mode"] = settings.persistence_mode
        snapshot["runtime_state_durable"] = not is_runtime_only_mode()
        snapshot["history_sampled"] = False
        snapshot["robot_flow_persistence_enabled"] = bool(
            settings.robot_flow_persistence_enabled
        )
        snapshot["robot_flow_effective_policy"] = (
            "durable"
            if settings.persistence_mode != "memory"
            or settings.robot_flow_persistence_enabled
            else "synthetic_ephemeral"
        )
        snapshot["robot_flow_actual_fill_policy"] = "durable"
        snapshot["robot_flow_synthetic_kline_enabled"] = True
        snapshot["robot_flow_synthetic_kline_canonical"] = False
        snapshot["robot_flow_synthetic_kline_intervals_persisted"] = ["1m", "5m"]
        snapshot["robot_flow_persistence_dropped_metrics_legacy"] = True
        snapshot["queue_size"] = self._queue.qsize()
        snapshot["pending_append_receipts"] = len(self._append_receipts)
        snapshot["append_batches_inflight"] = len(self._append_jobs)
        snapshot["materialization_lag"] = self.materialization_lag()
        snapshot["dead_letter"] = len(self._dead_letter)
        snapshot["watermarks"] = dict(self._watermarks)
        snapshot["watermark_domains"] = {"durable_seq": "domain_event_log_id", "materialized_seq": "domain_event_log_id",
                                        "critical_materialized_seq": "domain_event_log_id", "ingress_seq": "core_ingress_max",
                                        "matched_seq": "core_ingress_max", "published_seq": "core_ingress_max"}
        snapshot["ingress_seq"] = self._watermarks["ingress_seq"]
        snapshot["matched_seq"] = self._watermarks["matched_seq"]
        snapshot["durable_seq"] = self._watermarks["durable_seq"]
        snapshot["materialized_seq"] = self._watermarks["materialized_seq"]
        snapshot["published_seq"] = self._watermarks["published_seq"]
        snapshot["critical_materialized_seq"] = self._watermarks["critical_materialized_seq"]
        snapshot["non_critical_materialized_seq"] = self._watermarks["non_critical_materialized_seq"]
        snapshot["critical_sink"] = self.critical_sink_status()
        snapshot["status"] = "HALTED" if self._critical_blocked else snapshot.get("status", "HEALTHY")
        snapshot["command_queue_depth"] = self._queue.qsize()
        snapshot["journal_queue_depth"] = self._queue.qsize()
        snapshot["journal_queue_oldest_ms"] = 0
        snapshot["materialization_lag"] = max(
            snapshot["materialization_lag"],
            max(0, self._watermarks["durable_seq"] - self._watermarks["materialized_seq"]),
        )
        return snapshot
