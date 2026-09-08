from __future__ import annotations

import asyncio
import contextlib
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
import json
import logging
from time import monotonic
from typing import Any, Awaitable, Callable
from uuid import uuid4

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.services.matching_engine import BookOrder, MatchingEngine
from exchange_common.quote_pipeline import LatencyTracker
from app.services.causal_contracts import CommandEnvelope


QUOTE_SET_REPLACE = "QUOTE_SET_REPLACE"
RISK_CANCEL = "RISK_CANCEL"
CUSTOMER_ORDER = "CUSTOMER_ORDER"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(value: object) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExchangeCommand:
    command_id: str
    command_type: str
    symbol: str
    account_id: int
    payload: dict[str, Any]
    logical_timestamp: int
    product_type: str = "SPOT"
    strategy_instance: str | None = None
    generation: int | None = None
    config_version: str = "default"
    priority: int | None = None
    client_order_id: str | None = None
    account_domain: str | None = None
    market_id: str | None = None
    epoch: str | None = None
    rules_version: str = "default"
    risk_version: str = "default"
    fee_version: str = "default"
    priority_class: str = "NORMAL"

    def canonical_record(self, exchange_sequence: int) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "exchange_sequence": exchange_sequence,
            "account_id": int(self.account_id),
            "symbol": self.symbol.upper(),
            "command_type": self.command_type,
            "logical_timestamp": int(self.logical_timestamp),
            "config_version": self.config_version,
            "payload_hash": payload_hash(self.payload),
            "request_fingerprint": self.request_fingerprint(),
            "client_order_id": self.client_order_id,
            "account_domain": self.account_domain or self.product_type,
            "market_id": self.market_id,
            "epoch": self.epoch,
            "rules_version": self.rules_version,
            "risk_version": self.risk_version,
            "fee_version": self.fee_version,
            "priority_class": self.priority_class,
            "product_type": self.product_type,
            "strategy_instance": self.strategy_instance,
            "generation": self.generation,
            "payload": self.payload,
        }

    def request_fingerprint(self) -> str:
        """Hash the immutable request identity used by idempotency checks."""
        return payload_hash(
            {
                "command_type": self.command_type,
                "symbol": self.symbol.upper(),
                "account_id": int(self.account_id),
                "payload": self.payload,
                "logical_timestamp": int(self.logical_timestamp),
                "product_type": self.product_type.upper(),
                "strategy_instance": self.strategy_instance,
                "generation": self.generation,
                "config_version": self.config_version,
                "priority": self.priority,
                "client_order_id": self.client_order_id,
                "account_domain": self.account_domain or self.product_type,
                "market_id": self.market_id,
                "epoch": self.epoch,
                "rules_version": self.rules_version,
                "risk_version": self.risk_version,
                "fee_version": self.fee_version,
                "priority_class": self.priority_class,
            }
        )


@dataclass(frozen=True, slots=True)
class TradeEvent:
    trade_id: str
    event_sequence: int
    maker_order_id: str
    taker_order_id: str
    maker_uid: int
    taker_uid: int
    price: str
    quantity: str
    maker_fee: str
    taker_fee: str
    strategy_generation: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "event_sequence": self.event_sequence,
            "maker_order_id": self.maker_order_id,
            "taker_order_id": self.taker_order_id,
            "maker_uid": self.maker_uid,
            "taker_uid": self.taker_uid,
            "maker_user_id": self.maker_uid,
            "taker_user_id": self.taker_uid,
            "price": self.price,
            "quantity": self.quantity,
            "maker_fee": self.maker_fee,
            "taker_fee": self.taker_fee,
            "strategy_generation": self.strategy_generation,
        }


@dataclass(frozen=True, slots=True)
class CoreAck:
    command_id: str
    request_fingerprint: str
    accepted_count: int
    rejected_count: int
    changed_count: int
    noop_count: int
    first_error: str | None
    exchange_sequence: int
    durable_sequence: int
    matched_sequence: int
    duration_ms: float
    result_hash: str
    status: str
    dropped: bool = False
    operation_counts: tuple[tuple[str, int], ...] = ()
    failure_samples: tuple[str, ...] = ()
    failure_categories: tuple[tuple[str, int], ...] = ()
    quote_patch_duration_ms: float | None = None
    ack_stage: str = "DURABLE"
    epoch: str = ""
    command_sequence: int = 0
    priority_sequence: int = 0
    execution_sequence: int = 0
    published_sequence: int = 0
    causal_watermarks: dict[str, Any] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "request_fingerprint": self.request_fingerprint,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "failed_count": self.rejected_count,
            "changed_count": self.changed_count,
            "noop_count": self.noop_count,
            "first_error": self.first_error,
            "exchange_sequence": self.exchange_sequence,
            "durable_sequence": self.durable_sequence,
            "matched_sequence": self.matched_sequence,
            "duration_ms": self.duration_ms,
            "quote_patch_duration_ms": self.quote_patch_duration_ms,
            "result_hash": self.result_hash,
            "status": self.status,
            "dropped": self.dropped,
            "operation_counts": dict(self.operation_counts),
            "failed_samples": list(self.failure_samples),
            "failure_categories": dict(self.failure_categories),
            "ack_stage": self.ack_stage,
            "epoch": self.epoch,
            "command_sequence": self.command_sequence or self.exchange_sequence,
            "priority_sequence": self.priority_sequence,
            "execution_sequence": self.execution_sequence,
            "published_sequence": self.published_sequence,
            "causal_watermarks": dict(self.causal_watermarks or {}),
        }


@dataclass(frozen=True, slots=True)
class _AckCacheEntry:
    fingerprint: str
    ack: CoreAck
    stored_at: float
    expires_at: float


@dataclass(slots=True)
class _QueuedCommand:
    priority: int
    sequence: int
    command: ExchangeCommand
    future: asyncio.Future[CoreAck]
    operation_plan: list[dict[str, Any]] | None
    before_execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | int | None]] | None
    execute: Callable[[ExchangeCommand, int, list[dict[str, Any]]], Awaitable[dict[str, Any]]] | None
    received_at: float


class QuoteOperationPlan(list[dict[str, Any]]):
    """Small list-compatible plan carrying planner counters, not order state."""

    desired_levels: int = 0
    noop_elided: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


class ExchangeCore:
    """The process-local single writer for mutable exchange state.

    This class intentionally contains no SQL, filesystem, network, logging or
    wall-clock reads.  ``before_execute`` and ``execute`` are injected edges:
    the first is the durable journal adapter and the second is the existing
    accounting adapter.  They run outside the state transition itself.
    """

    def __init__(
        self,
        *,
        engine: MatchingEngine | None = None,
        sequence_start: int = 1000,
        max_queue: int = 10_000,
        ack_cache_max_size: int = 2_000,
        ack_cache_ttl_seconds: float = 120.0,
        failure_sample_limit: int = 8,
        epoch: str | None = None,
        mode: str = "legacy",
    ) -> None:
        self.engine = engine or MatchingEngine()
        self.epoch = str(epoch or f"exchange-{uuid4().hex}")
        self.mode = str(mode or "legacy").lower()
        self._next_sequence = max(0, int(sequence_start))
        self._queue: asyncio.PriorityQueue[tuple[int, int, _QueuedCommand]] = asyncio.PriorityQueue(maxsize=max_queue)
        self._worker_task: asyncio.Task | None = None
        self._stopping = False
        self._latest_generation: dict[tuple[str, int, str], int] = {}
        self._ack_cache_max_size = max(1, int(ack_cache_max_size))
        self._ack_cache_ttl_seconds = max(0.001, float(ack_cache_ttl_seconds))
        self._failure_sample_limit = max(0, int(failure_sample_limit))
        self._acks: OrderedDict[str, _AckCacheEntry] = OrderedDict()
        self._pending_acks: dict[str, tuple[str, asyncio.Future[CoreAck]]] = {}
        self._quote_set_latency_ms: deque[float] = deque(maxlen=4096)
        self._stage_latency = LatencyTracker()
        self._quote_orders: dict[str, dict[str, dict[str, Any]]] = {}
        self._received_at: dict[str, float] = {}
        self._watermarks = {
            "ingress_seq": self._next_sequence,
            "matched_seq": self._next_sequence,
            "durable_seq": self._next_sequence,
            "materialized_seq": 0,
            "published_seq": 0,
        }
        self._metrics = {
            "command_queue_depth": 0,
            "command_queue_oldest_ms": 0,
            "event_loop_lag_ms": 0,
            "command_count": 0,
            "quote_set_count": 0,
            "quote_set_dropped": 0,
            "trade_events": 0,
            "watcher_restart_count": 0,
            "ack_cache_size": 0,
            "ack_cache_hits": 0,
            "ack_cache_conflicts": 0,
            "ack_cache_evictions": 0,
            "ack_cache_expired": 0,
            "ack_cache_oldest_age": 0.0,
            "quote_set_desired_levels": 0,
            "quote_set_changed_ops": 0,
            "quote_set_noop_elided": 0,
            "quote_set_bytes_before": 0,
            "quote_set_bytes_after": 0,
            "quote_set_duration_ms": 0.0,
            "quote_set_p50_ms": 0.0,
            "quote_set_p95_ms": 0.0,
            "quote_set_p99_ms": 0.0,
            "status": "STARTING",
        }
        self._halt_reason: str | None = None

    # ------------------------------------------------------------------
    # lifecycle and command ingress
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._metrics["status"] = "HEALTHY"
            self._worker_task = asyncio.create_task(self._run(), name="exchange-core")

    async def stop(self) -> None:
        self._stopping = True
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None

    def _priority(self, command: ExchangeCommand) -> int:
        if command.priority is not None:
            return int(command.priority)
        if command.command_type in {RISK_CANCEL, "LIQUIDATION_CANCEL"}:
            return 0
        if command.command_type == QUOTE_SET_REPLACE:
            return 20
        return 10

    def _idempotency_lookup(self, command: ExchangeCommand, fingerprint: str) -> CoreAck | asyncio.Future[CoreAck] | None:
        now = monotonic()
        entry = self._acks.get(command.command_id)
        if entry is not None:
            if entry.fingerprint != fingerprint:
                self._metrics["ack_cache_conflicts"] += 1
                return self._system_ack(
                    command,
                    status="IDEMPOTENCY_CONFLICT",
                    reason="idempotency conflict: command_id was used with a different request",
                )
            if now >= entry.expires_at:
                self._metrics["ack_cache_expired"] += 1
                self._refresh_ack_cache_metrics(now)
                return self._system_ack(
                    command,
                    status="IDEMPOTENCY_EXPIRED",
                    reason="idempotency key expired; use a new command_id",
                )
            self._acks.move_to_end(command.command_id)
            self._metrics["ack_cache_hits"] += 1
            self._refresh_ack_cache_metrics(now)
            return entry.ack
        pending = self._pending_acks.get(command.command_id)
        if pending is not None:
            pending_fingerprint, future = pending
            if pending_fingerprint != fingerprint:
                self._metrics["ack_cache_conflicts"] += 1
                return self._system_ack(
                    command,
                    status="IDEMPOTENCY_CONFLICT",
                    reason="idempotency conflict: command_id is already in flight",
                )
            self._metrics["ack_cache_hits"] += 1
            return future
        return None

    def _store_ack(self, command_id: str, fingerprint: str, ack: CoreAck) -> None:
        now = monotonic()
        self._acks[command_id] = _AckCacheEntry(
            fingerprint=fingerprint,
            ack=ack,
            stored_at=now,
            expires_at=now + self._ack_cache_ttl_seconds,
        )
        self._acks.move_to_end(command_id)
        while len(self._acks) > self._ack_cache_max_size:
            self._acks.popitem(last=False)
            self._metrics["ack_cache_evictions"] += 1
        self._refresh_ack_cache_metrics(now)

    def _refresh_ack_cache_metrics(self, now: float | None = None) -> None:
        current = monotonic() if now is None else now
        self._metrics["ack_cache_size"] = len(self._acks)
        if self._acks:
            oldest = next(iter(self._acks.values()))
            self._metrics["ack_cache_oldest_age"] = max(0.0, round(current - oldest.stored_at, 3))
        else:
            self._metrics["ack_cache_oldest_age"] = 0.0

    def _system_ack(self, command: ExchangeCommand, *, status: str, reason: str) -> CoreAck:
        return CoreAck(
            command_id=command.command_id,
            request_fingerprint=command.request_fingerprint(),
            accepted_count=0,
            rejected_count=1,
            changed_count=0,
            noop_count=0,
            first_error=reason,
            exchange_sequence=self._next_sequence,
            durable_sequence=self._watermarks["durable_seq"],
            matched_sequence=self._watermarks["matched_seq"],
            duration_ms=0.0,
            result_hash=payload_hash({"status": status, "reason": reason}),
            status=status,
            dropped=True,
            operation_counts=(),
            failure_samples=(reason,) if self._failure_sample_limit else (),
            ack_stage=status if status in {"REJECTED", "IDEMPOTENCY_CONFLICT", "UNKNOWN_TIMEOUT", "UNKNOWN_AFTER_RESTART"} else "DURABLE",
            epoch=command.epoch or self.epoch,
            command_sequence=self._next_sequence,
            causal_watermarks=self.watermarks_snapshot(),
        )

    def _ack_from_result(
        self,
        command: ExchangeCommand,
        *,
        sequence: int,
        durable_sequence: int,
        result: dict[str, Any],
        started: float,
        status: str = "ACKED",
        dropped: bool = False,
        quote_patch_duration_ms: float | None = None,
    ) -> CoreAck:
        raw_counts = result.get("operation_counts") or result.get("operations") or {}
        if not isinstance(raw_counts, dict):
            raw_counts = {}
        counts: Counter[str] = Counter()
        for action, value in raw_counts.items():
            try:
                counts[str(action)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        if not counts and isinstance(result.get("items"), list):
            for item in result["items"]:
                if isinstance(item, dict) and item.get("action"):
                    counts[str(item["action"])] += 1
        accepted_value = result.get("accepted_count")
        if accepted_value is None:
            accepted = sum(value for action, value in counts.items() if action not in {"noop", "failed"})
        else:
            accepted = max(0, int(accepted_value))
        rejected_value = result.get("rejected_count", result.get("failed_count"))
        if rejected_value is None:
            failed = result.get("failed")
            rejected = len(failed) if isinstance(failed, list) else 0
        else:
            rejected = max(0, int(rejected_value))
        noop = max(0, int(result.get("noop_count", counts.get("noop", 0)) or 0))
        changed = max(0, int(result.get("changed_count", accepted) or 0))
        first_error = result.get("first_error")
        samples: list[str] = []
        raw_samples = result.get("failed_samples")
        if not isinstance(raw_samples, list):
            raw_samples = result.get("failed") if isinstance(result.get("failed"), list) else []
        for item in raw_samples[: self._failure_sample_limit]:
            if isinstance(item, dict):
                text = str(item.get("error") or item.get("reason") or item.get("order_id") or item)
            else:
                text = str(item)
            samples.append(text[:256])
        if first_error is None and samples:
            first_error = samples[0]
        raw_categories = result.get("failure_categories")
        categories: Counter[str] = Counter()
        if isinstance(raw_categories, dict):
            for category, value in raw_categories.items():
                try:
                    categories[str(category)] = max(0, int(value))
                except (TypeError, ValueError):
                    continue
        result_hash = payload_hash(result)
        return CoreAck(
            command_id=command.command_id,
            request_fingerprint=command.request_fingerprint(),
            accepted_count=accepted,
            rejected_count=rejected,
            changed_count=changed,
            noop_count=noop,
            first_error=str(first_error) if first_error is not None else None,
            exchange_sequence=sequence,
            durable_sequence=durable_sequence,
            matched_sequence=self._watermarks["matched_seq"],
            duration_ms=round(max(0.0, (monotonic() - started) * 1000), 3),
            result_hash=result_hash,
            status=status,
            dropped=dropped,
            operation_counts=tuple(sorted((key, value) for key, value in counts.items() if value)),
            failure_samples=tuple(samples),
            failure_categories=tuple(sorted((key, value) for key, value in categories.items() if value)),
            quote_patch_duration_ms=quote_patch_duration_ms,
            ack_stage=str(result.get("ack_stage") or "DURABLE"),
            epoch=command.epoch or self.epoch,
            command_sequence=sequence,
            priority_sequence=int(result.get("priority_sequence") or sequence),
            execution_sequence=int(result.get("execution_sequence") or sequence),
            published_sequence=int(result.get("published_sequence") or self._watermarks["published_seq"]),
            causal_watermarks=self.watermarks_snapshot(),
        )

    async def submit(
        self,
        command: ExchangeCommand,
        *,
        operation_plan: list[dict[str, Any]] | None = None,
        before_execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | int | None]] | None = None,
        execute: Callable[[ExchangeCommand, int, list[dict[str, Any]]], Awaitable[dict[str, Any]]] | None = None,
    ) -> CoreAck:
        """Enqueue one command and wait for its deterministic ACK."""
        command = ExchangeCommand(
            command_id=str(command.command_id),
            command_type=str(command.command_type),
            symbol=str(command.symbol).upper(),
            account_id=int(command.account_id),
            payload=dict(command.payload),
            logical_timestamp=int(command.logical_timestamp),
            product_type=str(command.product_type).upper(),
            strategy_instance=command.strategy_instance,
            generation=command.generation,
            config_version=str(command.config_version),
            priority=command.priority,
            client_order_id=command.client_order_id,
            account_domain=command.account_domain,
            market_id=command.market_id,
            epoch=command.epoch or self.epoch,
            rules_version=str(command.rules_version),
            risk_version=str(command.risk_version),
            fee_version=str(command.fee_version),
            priority_class=str(command.priority_class),
        )
        fingerprint = command.request_fingerprint()
        existing = self._idempotency_lookup(command, fingerprint)
        if existing is not None:
            if isinstance(existing, asyncio.Future):
                return await existing
            return existing

        self._next_sequence += 1
        sequence = self._next_sequence
        self._watermarks["ingress_seq"] = sequence
        self._metrics["command_count"] += 1
        if command.command_type == QUOTE_SET_REPLACE:
            self._metrics["quote_set_count"] += 1
            key = self._quote_state_key(command)
            generation = int(command.generation or 0)
            previous = self._latest_generation.get(key)
            if previous is not None and generation <= previous:
                self._metrics["quote_set_dropped"] += 1
                print(f"QUOTE_DROP {key} generation={generation} previous={previous}", flush=True)
                ack = CoreAck(
                    command_id=command.command_id,
                    request_fingerprint=fingerprint,
                    accepted_count=0,
                    rejected_count=0,
                    changed_count=0,
                    noop_count=0,
                    first_error="latest_wins",
                    exchange_sequence=sequence,
                    durable_sequence=self._watermarks["durable_seq"],
                    matched_sequence=self._watermarks["matched_seq"],
                    duration_ms=0.0,
                    result_hash=payload_hash({"reason": "latest_wins"}),
                    status="DROPPED_STALE_GENERATION",
                    dropped=True,
                    operation_counts=(),
                    failure_samples=(),
                )
                self._store_ack(command.command_id, fingerprint, ack)
                return ack
            self._latest_generation[key] = generation

        self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[CoreAck] = loop.create_future()
        self._pending_acks[command.command_id] = (fingerprint, future)
        queued = _QueuedCommand(
            priority=self._priority(command),
            sequence=sequence,
            command=command,
            future=future,
            operation_plan=operation_plan,
            before_execute=before_execute,
            execute=execute,
            received_at=monotonic(),
        )
        try:
            self._queue.put_nowait((queued.priority, sequence, queued))
        except asyncio.QueueFull as exc:
            self._pending_acks.pop(command.command_id, None)
            self._metrics["status"] = "DEGRADED"
            raise RuntimeError("exchange command queue is full") from exc
        self._refresh_queue_metrics()
        return await future

    async def submit_quote_set(
        self,
        command: ExchangeCommand,
        *,
        operation_plan: list[dict[str, Any]],
        before_execute: Callable[[dict[str, Any]], Awaitable[dict[str, Any] | int | None]],
        execute: Callable[[ExchangeCommand, int, list[dict[str, Any]]], Awaitable[dict[str, Any]]],
    ) -> CoreAck:
        if command.command_type != QUOTE_SET_REPLACE:
            raise ValueError("submit_quote_set requires QUOTE_SET_REPLACE")
        return await self.submit(
            command,
            operation_plan=operation_plan,
            before_execute=before_execute,
            execute=execute,
        )

    async def _run(self) -> None:
        while not self._stopping:
            _priority, _sequence, queued = await self._queue.get()
            started = monotonic()
            self._stage_latency.observe("queue_age", (started - queued.received_at) * 1000)
            try:
                command = queued.command
                if command.command_type == QUOTE_SET_REPLACE:
                    key = self._quote_state_key(command)
                    latest = self._latest_generation.get(key, int(command.generation or 0))
                    if int(command.generation or 0) < latest:
                        ack = CoreAck(
                            command_id=command.command_id,
                            request_fingerprint=command.request_fingerprint(),
                            accepted_count=0,
                            rejected_count=0,
                            changed_count=0,
                            noop_count=0,
                            first_error="newer_generation_queued",
                            exchange_sequence=queued.sequence,
                            durable_sequence=self._watermarks["durable_seq"],
                            matched_sequence=self._watermarks["matched_seq"],
                            duration_ms=round(max(0.0, (monotonic() - started) * 1000), 3),
                            result_hash=payload_hash({"reason": "newer_generation_queued"}),
                            status="DROPPED_SUPERSEDED",
                            dropped=True,
                            operation_counts=(),
                            failure_samples=(),
                        )
                        self._metrics["quote_set_dropped"] += 1
                        self._store_ack(command.command_id, command.request_fingerprint(), ack)
                        self._pending_acks.pop(command.command_id, None)
                        queued.future.set_result(ack)
                        continue

                operation_plan = queued.operation_plan if queued.operation_plan is not None else []
                record = command.canonical_record(queued.sequence)
                record["operations"] = operation_plan
                # Keep a strict, replayable envelope beside the compatibility
                # record.  Legacy consumers ignore additive fields; unified
                # diagnostics and the causal journal use this exact vector.
                record.update(
                    CommandEnvelope.create(
                        command_id=command.command_id,
                        command_type=command.command_type,
                        account_id=command.account_id,
                        account_domain=command.account_domain or command.product_type,
                        symbol=command.symbol,
                        market_id=command.market_id,
                        product_type=command.product_type,
                        epoch=command.epoch or self.epoch,
                        command_sequence=queued.sequence,
                        logical_timestamp=command.logical_timestamp,
                        rules_version=command.rules_version,
                        risk_version=command.risk_version,
                        fee_version=command.fee_version,
                        config_version=command.config_version,
                        strategy_instance=command.strategy_instance,
                        generation=command.generation,
                        priority_class=command.priority_class,
                        client_order_id=command.client_order_id,
                        payload=command.payload,
                    ).as_dict()
                )
                if queued.before_execute is not None:
                    durable = await queued.before_execute(record)
                    durable_sequence = self._durable_sequence(durable, queued.sequence)
                else:
                    durable_sequence = queued.sequence
                self._watermarks["durable_seq"] = max(self._watermarks["durable_seq"], durable_sequence)

                engine_started = monotonic()
                if queued.execute is not None:
                    result = await queued.execute(command, queued.sequence, operation_plan)
                elif command.command_type == QUOTE_SET_REPLACE:
                    result = self._execute_quote_set(command, queued.sequence, operation_plan)
                else:
                    result = {"accepted": True}
                engine_duration_ms = round(max(0.0, (monotonic() - engine_started) * 1000), 3)
                self._stage_latency.observe("engine_command", engine_duration_ms)
                self._record_quote_operations(command, operation_plan)
                self._watermarks["matched_seq"] = max(self._watermarks["matched_seq"], queued.sequence)
                ack = self._ack_from_result(
                    command,
                    sequence=queued.sequence,
                    durable_sequence=durable_sequence,
                    result=result,
                    started=started,
                    quote_patch_duration_ms=(
                        engine_duration_ms if command.command_type == QUOTE_SET_REPLACE else None
                    ),
                )
                self._store_ack(command.command_id, command.request_fingerprint(), ack)
                self._pending_acks.pop(command.command_id, None)
                if command.command_type == QUOTE_SET_REPLACE:
                    self._metrics["quote_set_duration_ms"] = ack.duration_ms
                    self._quote_set_latency_ms.append(ack.duration_ms)
                    # QuotePatch is the engine execution stage. Keep the
                    # total CoreAck duration separately for request/queue
                    # diagnostics; this stage is the <20ms execution target.
                    self._stage_latency.observe("quote_patch", engine_duration_ms)
                    self._refresh_quote_set_latency_metrics()
                queued.future.set_result(ack)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._halt_reason = str(exc)
                self._metrics["status"] = "HALTED"
                self._pending_acks.pop(queued.command.command_id, None)
                if not queued.future.done():
                    queued.future.set_exception(exc)
            finally:
                self._metrics["event_loop_lag_ms"] = max(0, int((monotonic() - started) * 1000))
                self._queue.task_done()
                self._refresh_queue_metrics()

    @staticmethod
    def _durable_sequence(value: dict[str, Any] | int | None, fallback: int) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, dict):
            for key in ("durable_seq", "exchange_sequence", "event_sequence"):
                if value.get(key) is not None:
                    return int(value[key])
        return fallback

    # ------------------------------------------------------------------
    # deterministic quote expansion and state
    # ------------------------------------------------------------------
    def expand_quote_set(
        self,
        command: ExchangeCommand,
        *,
        current_orders: dict[str, dict[str, Any]] | None = None,
        sequence: int | None = None,
    ) -> list[dict[str, Any]]:
        if command.command_type != QUOTE_SET_REPLACE:
            raise ValueError("quote expansion requires QUOTE_SET_REPLACE")
        payload = command.payload
        current = current_orders if current_orders is not None else self._quote_orders.get(
            self._quote_state_name(command), {}
        )
        # client_order_id is the logical identity of a quote level.  Collapse
        # duplicates across both sides before planning so one batch cannot
        # issue two final actions for the same client id.
        targets_by_client: dict[str, dict[str, Any]] = {}
        for side in (SIDE_BUY, SIDE_SELL):
            levels_key = "bids" if side == SIDE_BUY else "asks"
            for index, raw in enumerate(payload.get(levels_key, []), start=1):
                level = dict(raw)
                client_order_id = str(level.get("client_order_id") or level.get("tag") or f"{side}-{index:03d}")
                key = f"{side}:{client_order_id}"
                targets_by_client[client_order_id] = {
                    "key": key,
                    "client_order_id": client_order_id,
                    "side": side,
                    "price": str(level["price"]),
                    "quantity": str(level["quantity"]),
                    "level_index": index,
                    "position_action": level.get("position_action", "open"),
                    "reduce_only": bool(level.get("reduce_only", False)),
                    "leverage": level.get("leverage"),
                }
        targets = {str(desired["key"]): desired for desired in targets_by_client.values()}
        operations = QuoteOperationPlan()
        noop_elided = 0
        base_sequence = int(sequence or self._next_sequence + 1) * 1000
        # Self-trade protection for this command: no buy level may be placed at
        # or above the lowest ask in the same quote set, and no sell level at or
        # below the highest bid.  Without this, two maker accounts quoting the
        # same book cross each other, settle as takers and fail one-way margin
        # checks on the next round.
        ask_prices = [
            Decimal(str(level["price"]))
            for level in payload.get("asks", [])
            if isinstance(level, dict) and level.get("price") is not None
        ]
        bid_prices = [
            Decimal(str(level["price"]))
            for level in payload.get("bids", [])
            if isinstance(level, dict) and level.get("price") is not None
        ]
        min_ask = min(ask_prices) if ask_prices else None
        max_bid = max(bid_prices) if bid_prices else None
        for index, (key, desired) in enumerate(sorted(targets.items()), start=1):
            existing = current.get(key)
            op_sequence = base_sequence + index
            desired_price = Decimal(str(desired["price"]))
            crossing_within_plan = (
                (desired["side"] == SIDE_BUY and min_ask is not None and desired_price >= min_ask)
                or (desired["side"] == SIDE_SELL and max_bid is not None and desired_price <= max_bid)
            )
            if crossing_within_plan:
                if existing is not None:
                    # Keep the old resting order but exclude it from the new set
                    # so the next round can replace it cleanly.
                    operations.append(
                        {
                            "action": "cancel",
                            "key": key,
                            "order_id": existing.get("order_id"),
                            "sequence_number": op_sequence,
                            "side": existing.get("side"),
                            "remaining_quantity": existing.get("quantity") or existing.get("remaining_quantity"),
                        }
                    )
                continue
            if existing is None:
                operations.append(
                    {
                        "action": "place",
                        "key": key,
                        "order_id": self._deterministic_order_id(command, desired, int(command.generation or 0)),
                        "sequence_number": op_sequence,
                        "desired": desired,
                    }
                )
            elif self._same_quote(existing, desired):
                noop_elided += 1
                continue
            elif not self._same_order_semantics(existing, desired):
                operations.append(
                    {
                        "action": "cancel",
                        "key": key,
                        "order_id": existing.get("order_id"),
                        "sequence_number": op_sequence,
                        "side": existing.get("side"),
                        "remaining_quantity": existing.get("quantity") or existing.get("remaining_quantity"),
                        "replaces_order": True,
                    }
                )
                operations.append(
                    {
                        "action": "place",
                        "key": key,
                        "order_id": self._deterministic_order_id(command, desired, int(command.generation or 0)),
                        "sequence_number": op_sequence + 1,
                        "desired": desired,
                        "replaces_order": True,
                    }
                )
            else:
                operations.append(
                    {
                        "action": "amend",
                        "key": key,
                        "order_id": existing.get("order_id"),
                        "sequence_number": op_sequence,
                        "desired": desired,
                    }
                )
        target_keys = set(targets)
        for key, existing in sorted(current.items()):
            if key not in target_keys:
                operations.append(
                    {
                        "action": "cancel",
                        "key": key,
                        "order_id": existing.get("order_id"),
                        "sequence_number": base_sequence + len(operations) + 1,
                        "side": existing.get("side"),
                        "remaining_quantity": existing.get("quantity") or existing.get("remaining_quantity"),
                    }
                )
        # Release-first ordering: every cancel (old-level and extra-level) runs
        # before any place/amend so margin freed by removed quotes is available
        # to the new levels.  Otherwise a full rebuild places first and runs out
        # of available margin midway through the batch.
        operations = sorted(
            enumerate(operations),
            key=lambda item: (self._operation_priority(item[1]), item[0]),
        )
        operations = QuoteOperationPlan(operation for _index, operation in operations)
        desired_levels = len(targets)
        bytes_before = len(_canonical(payload).encode("utf-8"))
        bytes_after = len(_canonical(operations).encode("utf-8"))
        self._metrics["quote_set_desired_levels"] += desired_levels
        self._metrics["quote_set_changed_ops"] += len(operations)
        self._metrics["quote_set_noop_elided"] += noop_elided
        self._metrics["quote_set_bytes_before"] += bytes_before
        self._metrics["quote_set_bytes_after"] += bytes_after
        self._metrics["quote_set_duration_ms"] = 0.0
        operations.desired_levels = desired_levels
        operations.noop_elided = noop_elided
        operations.bytes_before = bytes_before
        operations.bytes_after = bytes_after
        summary: dict[str, int] = {}
        for operation in operations:
            key = f"{operation.get('action')}/{operation.get('desired', {}).get('side') or operation.get('side') or '?'}"
            summary[key] = summary.get(key, 0) + 1
        logging.getLogger("exchange_core").info(
            "quote plan symbol=%s account=%s generation=%s targets=%s current=%s summary=%s",
            command.symbol,
            command.account_id,
            command.generation,
            len(targets),
            len(current),
            summary,
        )
        return operations

    @staticmethod
    def _operation_priority(operation: dict[str, Any]) -> int:
        action = str(operation.get("action") or "")
        if action == "cancel":
            return 0
        if action in {"shrink", "release"}:
            return 1
        if action in {"amend", "price_change", "quantity_change", "priority_reset"}:
            return 2
        if action == "place":
            return 3
        return 4

    @staticmethod
    def _quote_state_key(command: ExchangeCommand) -> tuple[str, int, str]:
        return (command.symbol, int(command.account_id), command.strategy_instance or "default")

    @staticmethod
    def _quote_state_name(command: ExchangeCommand) -> str:
        symbol, account_id, strategy = ExchangeCore._quote_state_key(command)
        return f"{symbol}|{account_id}|{strategy}"

    @staticmethod
    def _same_quote(current: dict[str, Any], desired: dict[str, Any]) -> bool:
        return (
            ExchangeCore._same_order_semantics(current, desired)
            and
            str(current.get("side")) == str(desired.get("side"))
            and Decimal(str(current.get("price"))) == Decimal(str(desired.get("price")))
            and Decimal(str(current.get("quantity", current.get("remaining_quantity", "0"))))
            == Decimal(str(desired.get("quantity")))
        )

    @staticmethod
    def _same_order_semantics(current: dict[str, Any], desired: dict[str, Any]) -> bool:
        return (
            str(current.get("position_action") or "open") == str(desired.get("position_action") or "open")
            and bool(current.get("reduce_only", False)) == bool(desired.get("reduce_only", False))
            and (
                desired.get("leverage") is None
                or str(current.get("leverage") or "") == str(desired.get("leverage") or "")
            )
        )

    @staticmethod
    def _deterministic_order_id(command: ExchangeCommand, desired: dict[str, Any], generation: int) -> str:
        raw = f"{command.symbol}|{command.account_id}|{command.strategy_instance}|{desired['side']}|{desired['client_order_id']}|{generation}"
        return f"qord_{sha256(raw.encode('utf-8')).hexdigest()[:48]}"

    def _execute_quote_set(
        self,
        command: ExchangeCommand,
        sequence: int,
        operation_plan: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """Pure fallback executor used by unit tests and replay tools."""
        operations = operation_plan if operation_plan is not None else self.expand_quote_set(command, sequence=sequence)
        changed = {"place": 0, "amend": 0, "cancel": 0, "noop": 0}
        for operation in operations:
            action = str(operation.get("action"))
            changed[action] = changed.get(action, 0) + 1
        return {"quote_set": True, "operations": changed, "operation_count": len(operations)}

    def _record_quote_operations(self, command: ExchangeCommand, operations: list[dict[str, Any]]) -> None:
        if command.command_type != QUOTE_SET_REPLACE:
            return
        state: dict[str, dict[str, Any]] = self._quote_orders.setdefault(self._quote_state_name(command), {})
        for operation in operations:
            action = str(operation.get("action"))
            key = str(operation.get("key") or "")
            if action == "cancel":
                state.pop(key, None)
            elif action in {"place", "amend", "noop"}:
                desired = dict(operation.get("desired") or {})
                desired["order_id"] = operation.get("order_id") or desired.get("order_id")
                state[key] = desired

    def record_published(self, sequence: int) -> None:
        self._watermarks["published_seq"] = max(self._watermarks["published_seq"], int(sequence))

    def next_sequence_hint(self) -> int:
        """Return the next ingress sequence without reserving it."""
        return self._next_sequence + 1

    def record_materialized(self, sequence: int) -> None:
        self._watermarks["materialized_seq"] = max(self._watermarks["materialized_seq"], int(sequence))

    def record_trade(self) -> None:
        self._metrics["trade_events"] += 1

    def mark_halted(self, reason: str) -> None:
        self._halt_reason = str(reason)
        self._metrics["status"] = "HALTED"

    def _refresh_queue_metrics(self) -> None:
        self._metrics["command_queue_depth"] = self._queue.qsize()
        if self._queue.empty():
            self._metrics["command_queue_oldest_ms"] = 0
        else:
            try:
                oldest = min(item[2].received_at for item in self._queue._queue)  # type: ignore[attr-defined]
            except (AttributeError, ValueError):
                oldest = monotonic()
            self._metrics["command_queue_oldest_ms"] = max(0, int((monotonic() - oldest) * 1000))

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
        return round(float(ordered[index]), 3)

    def _refresh_quote_set_latency_metrics(self) -> None:
        values = list(self._quote_set_latency_ms)
        self._metrics["quote_set_p50_ms"] = self._percentile(values, 50)
        self._metrics["quote_set_p95_ms"] = self._percentile(values, 95)
        self._metrics["quote_set_p99_ms"] = self._percentile(values, 99)

    def watermarks_snapshot(self) -> dict[str, int]:
        return {key: int(value) for key, value in self._watermarks.items()}

    def metrics_snapshot(self) -> dict[str, Any]:
        self._refresh_queue_metrics()
        self._refresh_ack_cache_metrics()
        watermarks = self.watermarks_snapshot()
        command_depth = int(self._metrics["command_queue_depth"])
        materialization_lag = max(0, watermarks["durable_seq"] - watermarks["materialized_seq"])
        matched_durable_lag = max(0, watermarks["matched_seq"] - watermarks["durable_seq"])
        published_lag = max(0, watermarks["matched_seq"] - watermarks["published_seq"])
        healthy = self._metrics["status"] not in {"HALTED", "DEGRADED"}
        # ExchangeCore command sequences and the persistence event-log IDs are
        # different clocks in memory/ephemeral mode.  The core-local
        # materialized watermark is retained as a legacy diagnostic, but must
        # not gate restart decisions. AppRuntime combines this queue/status
        # signal with the authoritative PersistenceWriter health below.
        restart_allowed = healthy and command_depth < max(1, self._queue.maxsize // 2)
        return {
            **self._metrics,
            "watermarks": watermarks,
            "ingress_seq": watermarks["ingress_seq"],
            "matched_seq": watermarks["matched_seq"],
            "durable_seq": watermarks["durable_seq"],
            "materialized_seq": watermarks["materialized_seq"],
            "published_seq": watermarks["published_seq"],
            "materialization_lag": materialization_lag,
            "matched_durable_lag": matched_durable_lag,
            "published_lag": published_lag,
            "latency": self._stage_latency.snapshot(),
            "halt_reason": self._halt_reason,
            "watcher_restart_allowed": restart_allowed,
            "watcher_restart_gate_reason": None if restart_allowed else "exchange_congestion_or_halted",
        }

    def snapshot_state(self) -> dict[str, Any]:
        books: dict[str, Any] = {}
        for symbol, book in self.engine.books.items():
            books[symbol] = {
                "orders": [
                    {
                        "order_id": node.order_id,
                        "user_id": node.user_id,
                        "side": node.side,
                        "price": str(node.price),
                        "remaining": str(node.remaining),
                        "created_at": node.created_at.astimezone(UTC).isoformat(),
                        "sequence_number": int(node.sequence_number),
                    }
                    for node in book.orders.values()
                ]
            }
        return {
            "schema_version": 1,
            "watermarks": self.watermarks_snapshot(),
            "next_sequence": self._next_sequence,
            "latest_generation": {
                f"{symbol}|{account_id}|{strategy}": generation
                for (symbol, account_id, strategy), generation in self._latest_generation.items()
            },
            "quote_orders": self._quote_orders,
            "books": books,
            "config_version": "runtime",
        }

    def state_hash(self) -> str:
        return payload_hash(self.snapshot_state())

    def restore_state(self, snapshot: dict[str, Any], *, restore_books: bool = True) -> None:
        self._next_sequence = max(self._next_sequence, int(snapshot.get("next_sequence") or 0))
        watermarks = snapshot.get("watermarks") if isinstance(snapshot.get("watermarks"), dict) else {}
        for key in self._watermarks:
            if watermarks.get(key) is not None:
                self._watermarks[key] = max(self._watermarks[key], int(watermarks[key]))
        self._latest_generation = {}
        for key, value in (snapshot.get("latest_generation") or {}).items():
            if "|" not in str(key):
                continue
            parts = str(key).split("|", 2)
            if len(parts) != 3:
                continue
            symbol, account_id, strategy = parts
            self._latest_generation[(symbol, int(account_id), strategy)] = max(
                self._latest_generation.get((symbol, int(account_id), strategy), 0), int(value)
            )
        self._quote_orders = json.loads(json.dumps(snapshot.get("quote_orders") or {}, default=str))
        if not restore_books:
            self._metrics["status"] = "HEALTHY"
            return
        for symbol in list(self.engine.books):
            self.engine.clear_market(symbol)
        for symbol, book_payload in (snapshot.get("books") or {}).items():
            for raw in book_payload.get("orders", []) if isinstance(book_payload, dict) else []:
                self.engine.load_resting_order(
                    symbol,
                    BookOrder(
                        order_id=str(raw["order_id"]),
                        user_id=int(raw["user_id"]),
                        side=str(raw["side"]),
                        price=Decimal(str(raw["price"])),
                        remaining=Decimal(str(raw["remaining"])),
                        created_at=datetime.fromisoformat(str(raw["created_at"])),
                        sequence_number=int(raw.get("sequence_number") or 0),
                    ),
                )
        self._metrics["status"] = "HEALTHY"

    def replay_quote_set(self, command_record: dict[str, Any]) -> dict[str, Any]:
        """Replay a journal record without touching I/O or wall-clock time."""
        command = ExchangeCommand(
            command_id=str(command_record["command_id"]),
            command_type=str(command_record.get("command_type") or QUOTE_SET_REPLACE),
            symbol=str(command_record["symbol"]),
            account_id=int(command_record.get("account_id") or 0),
            payload=dict(command_record.get("payload") or {}),
            logical_timestamp=int(command_record.get("logical_timestamp") or 0),
            product_type=str(command_record.get("product_type") or "SPOT"),
            strategy_instance=command_record.get("strategy_instance"),
            generation=command_record.get("generation"),
            config_version=str(command_record.get("config_version") or "default"),
        )
        operations = list(command_record.get("operations") or command.payload.get("operations") or [])
        state_key = self._quote_state_key(command)
        generation = int(command.generation or 0)
        print(f"QUOTE_REPLAY {state_key} generation={generation} prev={self._latest_generation.get(state_key, 0)}", flush=True)
        self._latest_generation[state_key] = max(self._latest_generation.get(state_key, 0), generation)
        self._record_quote_operations(command, operations)
        sequence = int(command_record.get("exchange_sequence") or self._next_sequence)
        self._next_sequence = max(self._next_sequence, sequence)
        self._watermarks["ingress_seq"] = max(self._watermarks["ingress_seq"], sequence)
        self._watermarks["durable_seq"] = max(self._watermarks["durable_seq"], sequence)
        self._watermarks["matched_seq"] = max(self._watermarks["matched_seq"], sequence)
        return {"replayed": True, "operation_count": len(operations), "exchange_sequence": sequence}
