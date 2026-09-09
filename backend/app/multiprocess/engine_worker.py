from __future__ import annotations

from app.services.matching_faults import BusinessRejected, MatchingHalted, BookInvariantError

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import os
from queue import Empty, Full
import time
from typing import Any
from uuid import uuid4

from app.services.execution_events import EventOutbox
from app.services.matching_engine import BookOrder, BulkQuotePatchOperation, MatchingEngine
from app.services.sequencer import (
    AmendOrderCommand,
    BulkQuotePatchCommand,
    CancelOrderCommand,
    NewOrderCommand,
    SymbolSequencer,
)
from app.multiprocess.protocol import (
    IPCEnvelope,
    PublishedBookEvent,
    ReliableWorkerEvent,
    drain_latest,
    now_ms,
    put_latest,
)


USER_COMMAND_KINDS = frozenset({"PLACE", "CANCEL", "AMEND"})


def _queue_size(queue: Any) -> int:
    try:
        return max(0, int(queue.qsize()))
    except (AttributeError, NotImplementedError, OSError):
        return -1


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise BusinessRejected(f"invalid decimal {field}") from exc
    if not result.is_finite():
        raise BusinessRejected(f"non-finite decimal {field}")
    return result


@dataclass(slots=True)
class OrderMetadata:
    order_id: str
    client_order_id: str
    user_id: int
    request_fingerprint: str
    side: str
    product_type: str


class EngineWorkerRuntime:
    """One market's complete mutable state, owned by one process/thread.

    The process loop is the only caller of ``SymbolSequencer._execute``.  No
    thread pool and no shared mutable Python object can reach ``engine``.
    """

    def __init__(
        self,
        *,
        run_id: str,
        market_id: str,
        product_type: str,
        stream_epoch: str | None = None,
        command_cache_size: int = 20_000,
    ) -> None:
        self.run_id = str(run_id)
        self.market_id = str(market_id).upper()
        self.product_type = str(product_type).upper()
        self.stream_epoch = str(stream_epoch or uuid4())
        self.stream_id = f"{self.run_id}:{self.market_id}:{self.stream_epoch}"
        self.engine = MatchingEngine()
        self.event_outbox = EventOutbox(queue=asyncio.Queue(maxsize=4_096))
        self._sequence = 0
        self.sequencer = SymbolSequencer(
            symbol=self.market_id,
            engine=self.engine,
            outbox=self.event_outbox,
            next_sequence=self._next_sequence,
            observe_sequence=self._observe_sequence,
        )
        self.order_index: dict[str, OrderMetadata] = {}
        self.client_order_ids: dict[tuple[int, str], tuple[str, str, dict[str, Any]]] = {}
        self.test_accounts: dict[int, dict[str, Decimal]] = {}
        self.positions: dict[int, Decimal] = {}
        self.margin_state: dict[int, dict[str, Decimal]] = {}
        self.quote_patch_executor = {"latest_generation": -1, "dropped_stale": 0}
        self.maker_paused = False
        self.command_results: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self.command_cache_size = max(128, int(command_cache_size))
        self.queue_lag_samples: deque[float] = deque(maxlen=8_192)
        self.reference_state: dict[str, Any] = {
            "bid": None,
            "ask": None,
            "mid": None,
            "source_timestamp": 0,
            "received_at": 0,
            "source": "missing",
        }
        self.publish_seq = 0
        self.last_book_progress_at = 0
        self.book_progress_times: deque[int] = deque(maxlen=2_048)
        self._last_book: dict[str, list[list[str]]] = {"bids": [], "asks": []}
        self.metrics = {
            "commands": 0,
            "command_conflicts": 0,
            "deadline_expired": 0,
            "maker_plan_dropped": 0,
            "book_frames_dropped": 0,
            "history_samples_dropped": 0,
            "invariant_failures": 0,
            "private_events": 0,
            "queue_lag_ms_last": 0.0,
            "queue_lag_ms_max": 0.0,
        }

    def _next_sequence(self, _symbol: str) -> int:
        self._sequence += 1
        return self._sequence

    def _observe_sequence(self, _symbol: str, value: int | None) -> None:
        if value is not None:
            self._sequence = max(self._sequence, int(value))

    def _cache_result(self, envelope: IPCEnvelope, result: dict[str, Any]) -> dict[str, Any]:
        self.command_results[envelope.command_id] = (envelope.request_fingerprint, result)
        self.command_results.move_to_end(envelope.command_id)
        while len(self.command_results) > self.command_cache_size:
            self.command_results.popitem(last=False)
        return result

    def _system_result(self, envelope: IPCEnvelope, status: str, reason: str) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "command_id": envelope.command_id,
            "request_fingerprint": envelope.request_fingerprint,
            "market_id": self.market_id,
            "stream_epoch": self.stream_epoch,
            "stream_id": self.stream_id,
            "command_sequence": envelope.command_sequence,
            "priority_sequence": envelope.priority_sequence,
            "completed_at": now_ms(),
            "ipc_latency_ms": max(0, now_ms() - int(envelope.sent_at)),
        }

    def _idempotency_result(self, envelope: IPCEnvelope) -> dict[str, Any] | None:
        cached = self.command_results.get(envelope.command_id)
        if cached is None:
            return None
        fingerprint, result = cached
        self.command_results.move_to_end(envelope.command_id)
        if fingerprint == envelope.request_fingerprint:
            return dict(result)
        self.metrics["command_conflicts"] += 1
        return self._system_result(
            envelope,
            "IDEMPOTENCY_CONFLICT",
            "command_id was already used with a different request fingerprint",
        )

    def handle(self, envelope: IPCEnvelope) -> dict[str, Any]:
        envelope.validate(expected_run_id=self.run_id)
        queue_lag = float(max(0, now_ms() - int(envelope.sent_at)))
        self.queue_lag_samples.append(queue_lag)
        self.metrics["queue_lag_ms_last"] = queue_lag
        self.metrics["queue_lag_ms_max"] = max(float(self.metrics["queue_lag_ms_max"]), queue_lag)
        if envelope.market_id != self.market_id:
            return self._system_result(envelope, "MARKET_MISMATCH", "command routed to the wrong worker")
        if envelope.stream_epoch not in {"", "*", self.stream_epoch}:
            return self._system_result(envelope, "STALE_STREAM_EPOCH", "command targets an old worker epoch")
        cached = self._idempotency_result(envelope)
        if cached is not None:
            return cached
        if now_ms() > envelope.deadline:
            self.metrics["deadline_expired"] += 1
            return self._cache_result(
                envelope,
                self._system_result(envelope, "DEADLINE_EXPIRED_NOT_EXECUTED", "deadline elapsed before execution"),
            )

        self.metrics["commands"] += 1
        try:
            self.engine.fault.check()
            if envelope.kind in {"MAKER_PLAN", "QUOTE_PATCH"} and self.maker_paused:
                result = self._system_result(envelope, "REJECTED_MAKER_PAUSED", "maker ingress is paused by runtime readiness gate")
            elif envelope.kind == "PLACE":
                result = self._place(envelope)
            elif envelope.kind == "CANCEL":
                result = self._cancel(envelope)
            elif envelope.kind == "AMEND":
                result = self._amend(envelope)
            elif envelope.kind in {"MAKER_PLAN", "QUOTE_PATCH"}:
                result = self._quote_patch(envelope)
            elif envelope.kind == "SNAPSHOT_REQUEST":
                result = self._system_result(envelope, "ACKED", "snapshot requested")
                result["force_snapshot"] = True
            elif envelope.kind == "PAUSE_MAKER":
                self.maker_paused = True
                result = self._system_result(envelope, "ACKED", "maker plan ingress paused")
            elif envelope.kind == "RESUME_MAKER":
                self.maker_paused = False
                result = self._system_result(envelope, "ACKED", "maker plan ingress resumed")
            else:
                result = self._system_result(envelope, "REJECTED", f"unsupported command kind: {envelope.kind}")
        except MatchingHalted as exc:
            result = self._system_result(envelope, "NOT_EXECUTED", str(exc))
        except BusinessRejected as exc:
            result = self._system_result(envelope, "REJECTED", str(exc))
        except Exception as exc:
            self.engine.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            result = self._system_result(envelope, "UNKNOWN", str(exc))
        result["matching_gate"] = self.engine.fault.snapshot()
        result.setdefault("command_id", envelope.command_id)
        result.setdefault("request_fingerprint", envelope.request_fingerprint)
        result.setdefault("market_id", self.market_id)
        result.setdefault("stream_epoch", self.stream_epoch)
        result.setdefault("stream_id", self.stream_id)
        result.setdefault("command_sequence", envelope.command_sequence)
        result.setdefault("priority_sequence", envelope.priority_sequence)
        result.setdefault("completed_at", now_ms())
        result.setdefault("ipc_latency_ms", max(0, now_ms() - int(envelope.sent_at)))
        self._drain_execution_events()
        return self._cache_result(envelope, result)

    def _drain_execution_events(self) -> None:
        """Keep the process-local telemetry outbox bounded without I/O."""

        while True:
            try:
                self.event_outbox.queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self.event_outbox.queue.task_done()

    def _place(self, envelope: IPCEnvelope) -> dict[str, Any]:
        payload = envelope.payload
        user_id = int(payload.get("user_id") or 0)
        client_order_id = str(payload.get("client_order_id") or envelope.command_id)
        client_key = (user_id, client_order_id)
        existing = self.client_order_ids.get(client_key)
        if existing is not None:
            order_id, fingerprint, original = existing
            if fingerprint == envelope.request_fingerprint:
                return dict(original)
            self.metrics["command_conflicts"] += 1
            result = self._system_result(
                envelope,
                "CLIENT_ORDER_ID_CONFLICT",
                f"client_order_id already belongs to {order_id}",
            )
            result["order_id"] = order_id
            return result
        side = str(payload.get("side") or "").lower()
        if side not in {"buy", "sell"}:
            raise BusinessRejected("side must be buy or sell")
        price = _decimal(payload.get("price"), field="price")
        quantity = _decimal(payload.get("quantity"), field="quantity")
        if price <= 0 or quantity <= 0:
            raise BusinessRejected("price and quantity must be positive")
        order_id = str(payload.get("order_id") or f"{self.market_id}-{uuid4().hex}")
        sequenced = self.sequencer._execute(
            NewOrderCommand(
                order_id=order_id,
                user_id=user_id,
                side=side,
                quantity=quantity,
                created_at=datetime.now(tz=UTC),
                limit_price=price,
                can_rest=bool(payload.get("can_rest", True)),
                stp_account_key=str(payload.get("stp_account_key") or f"user:{user_id}"),
                stp_group_key=str(payload.get("stp_group_key") or "user"),
                stp_is_bot=bool(payload.get("is_bot", False)),
                stp_mode=str(payload.get("stp_mode") or "cancel_taker"),
            )
        )
        engine_result = sequenced.engine_result
        if engine_result is None:
            raise RuntimeError("sequencer returned no engine result")
        metadata = OrderMetadata(
            order_id=order_id,
            client_order_id=client_order_id,
            user_id=user_id,
            request_fingerprint=envelope.request_fingerprint,
            side=side,
            product_type=self.product_type,
        )
        if engine_result.placed_on_book:
            self.order_index[order_id] = metadata
        fills = self._fills(engine_result.fills, taker_user_id=user_id, taker_side=side)
        result = self._system_result(envelope, "ACKED", "executed")
        result.update(
            {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "sequence_number": sequenced.sequence_number,
                "placed_on_book": bool(engine_result.placed_on_book),
                "remaining_quantity": str(engine_result.remaining_quantity),
                "fills": fills,
                "private_events": [
                    {
                        "user_id": user_id,
                        "event": "order_update",
                        "order_id": order_id,
                        "status": "NEW" if engine_result.placed_on_book else "FILLED",
                    }
                ],
            }
        )
        self.client_order_ids[client_key] = (order_id, envelope.request_fingerprint, dict(result))
        self.metrics["private_events"] += len(result["private_events"])
        self._purge_order_index()
        self._validate_invariants()
        return result

    def _cancel(self, envelope: IPCEnvelope) -> dict[str, Any]:
        order_id = str(envelope.payload.get("order_id") or "")
        if not order_id:
            raise BusinessRejected("order_id is required")
        metadata = self.order_index.get(order_id)
        sequenced = self.sequencer._execute(CancelOrderCommand(order_id=order_id))
        if sequenced.cancelled_side is None:
            return self._system_result(envelope, "NOT_FOUND", "order not found on book")
        self.order_index.pop(order_id, None)
        result = self._system_result(envelope, "ACKED", "cancelled")
        result.update(
            {
                "order_id": order_id,
                "sequence_number": sequenced.sequence_number,
                "cancelled_remaining": str(sequenced.cancelled_remaining or 0),
                "private_events": [] if metadata is None else [
                    {
                        "user_id": metadata.user_id,
                        "event": "order_update",
                        "order_id": order_id,
                        "status": "CANCELED",
                    }
                ],
            }
        )
        self._validate_invariants()
        return result

    def _amend(self, envelope: IPCEnvelope) -> dict[str, Any]:
        payload = envelope.payload
        order_id = str(payload.get("order_id") or "")
        if not order_id:
            raise BusinessRejected("order_id is required")
        price = _decimal(payload.get("price"), field="price")
        quantity = _decimal(payload.get("quantity"), field="quantity")
        if price <= 0 or quantity <= 0:
            raise BusinessRejected("price and quantity must be positive")
        sequenced = self.sequencer._execute(
            AmendOrderCommand(order_id=order_id, new_price=price, new_remaining=quantity)
        )
        if sequenced.amend_result is None:
            return self._system_result(envelope, "NOT_FOUND", "order not found on book")
        result = self._system_result(envelope, "ACKED", "amended")
        result.update(
            {
                "order_id": order_id,
                "sequence_number": sequenced.sequence_number,
                "price": str(price),
                "quantity": str(quantity),
            }
        )
        self._validate_invariants()
        return result

    def _quote_patch(self, envelope: IPCEnvelope) -> dict[str, Any]:
        generation = int(envelope.payload.get("generation") or 0)
        latest = int(self.quote_patch_executor["latest_generation"])
        if generation <= latest:
            self.quote_patch_executor["dropped_stale"] += 1
            self.metrics["maker_plan_dropped"] += 1
            return self._system_result(envelope, "DROPPED_STALE_PLAN", "latest-wins maker plan")
        self.quote_patch_executor["latest_generation"] = generation
        operations = self._quote_operations(envelope)
        if not operations:
            result = self._system_result(envelope, "ACKED", "no quote changes")
            result.update({"generation": generation, "changed_count": 0, "noop_count": 1})
            return result
        sequenced = self.sequencer._execute(BulkQuotePatchCommand(operations=operations))
        patch = sequenced.bulk_quote_patch_result
        if patch is None:
            raise RuntimeError("sequencer returned no bulk quote patch result")
        self._sync_order_index(operations, envelope)
        result = self._system_result(envelope, "ACKED", "quote patch executed")
        result.update(
            {
                "generation": generation,
                "sequence_number": sequenced.sequence_number,
                "accepted_count": patch.accepted_count,
                "changed_count": patch.changed_count,
                "rejected_count": patch.rejected_count,
                "operation_counts": dict(patch.operation_counts),
                "fills": [
                    {**self._fills([fill], taker_user_id=taker[1], taker_side=taker[2])[0],
                     "taker_order_id": taker[0], "taker_side": taker[2]}
                    for fill, taker in zip(patch.fills, patch.fill_takers, strict=True)
                ],
                "stp_intercept_count": patch.stp_intercept_count,
                "stp_decremented_quantity": str(patch.stp_decremented_quantity),
            }
        )
        self._validate_invariants()
        return result

    def _purge_order_index(self) -> None:
        live_ids = set(self.engine.ensure_market(self.market_id).orders)
        for order_id in list(self.order_index):
            if order_id not in live_ids:
                self.order_index.pop(order_id, None)

    def _quote_operations(self, envelope: IPCEnvelope) -> list[BulkQuotePatchOperation]:
        payload = envelope.payload
        raw_operations = payload.get("operations")
        if isinstance(raw_operations, list):
            return [self._coerce_patch_operation(item, envelope) for item in raw_operations]

        user_id = int(payload.get("user_id") or 0)
        desired: dict[str, tuple[str, Decimal, Decimal]] = {}
        for side, key in (("buy", "bids"), ("sell", "asks")):
            for index, item in enumerate(payload.get(key) or [], start=1):
                if not isinstance(item, dict):
                    continue
                client_id = str(item.get("client_order_id") or item.get("tag") or f"{side}-{index}")
                desired[client_id] = (
                    side,
                    _decimal(item.get("price"), field="price"),
                    _decimal(item.get("quantity"), field="quantity"),
                )
        current = {
            metadata.client_order_id: metadata
            for metadata in self.order_index.values()
            if metadata.user_id == user_id
        }
        operations: list[BulkQuotePatchOperation] = []
        book = self.engine.ensure_market(self.market_id)
        for client_id, metadata in current.items():
            if client_id not in desired:
                operations.append(BulkQuotePatchOperation(action="cancel", order_id=metadata.order_id))
        for client_id, (side, price, quantity) in desired.items():
            metadata = current.get(client_id)
            if metadata is None:
                operations.append(
                    BulkQuotePatchOperation(
                        action="place",
                        order_id=f"{self.market_id}-{user_id}-{client_id}",
                        user_id=user_id,
                        side=side,
                        price=price,
                        quantity=quantity,
                        created_at=datetime.now(tz=UTC),
                        can_rest=True,
                        stp_account_key=f"user:{user_id}",
                        stp_group_key="bot",
                        stp_is_bot=True,
                    )
                )
                continue
            node = book.orders.get(metadata.order_id)
            if node is not None and (node.price != price or node.remaining != quantity):
                operations.append(
                    BulkQuotePatchOperation(
                        action="amend",
                        order_id=metadata.order_id,
                        user_id=user_id,
                        side=side,
                        price=price,
                        quantity=quantity,
                        created_at=datetime.now(tz=UTC),
                    )
                )
        return operations

    def _coerce_patch_operation(self, item: Any, envelope: IPCEnvelope) -> BulkQuotePatchOperation:
        if not isinstance(item, dict):
            raise BusinessRejected("quote patch operation must be an object")
        action = str(item.get("action") or "").lower()
        order_id = str(item.get("order_id") or "")
        if not order_id:
            raise BusinessRejected("quote patch order_id is required")
        return BulkQuotePatchOperation(
            action=action,
            order_id=order_id,
            user_id=int(item.get("user_id") or envelope.payload.get("user_id") or 0),
            side=str(item.get("side") or "").lower() or None,
            price=None if item.get("price") is None else _decimal(item.get("price"), field="price"),
            quantity=None if item.get("quantity") is None else _decimal(item.get("quantity"), field="quantity"),
            created_at=datetime.now(tz=UTC),
            can_rest=bool(item.get("can_rest", True)),
            stp_account_key=str(item.get("stp_account_key") or f"user:{int(item.get('user_id') or 0)}"),
            stp_group_key=str(item.get("stp_group_key") or "bot"),
            stp_is_bot=bool(item.get("is_bot", True)),
            stp_mode=str(item.get("stp_mode") or "cancel_taker"),
        )

    def _sync_order_index(self, operations: list[BulkQuotePatchOperation], envelope: IPCEnvelope) -> None:
        by_order = self.engine.ensure_market(self.market_id).orders
        for operation in operations:
            if operation.order_id not in by_order:
                self.order_index.pop(operation.order_id, None)
                continue
            existing = self.order_index.get(operation.order_id)
            if existing is not None:
                continue
            client_id = operation.order_id.split(f"{self.market_id}-{operation.user_id}-", 1)[-1]
            self.order_index[operation.order_id] = OrderMetadata(
                order_id=operation.order_id,
                client_order_id=client_id,
                user_id=operation.user_id,
                request_fingerprint=envelope.request_fingerprint,
                side=str(operation.side or by_order[operation.order_id].side),
                product_type=self.product_type,
            )
        live_ids = set(by_order)
        for order_id in list(self.order_index):
            if order_id not in live_ids:
                self.order_index.pop(order_id, None)

    def _fills(self, fills: list[Any], *, taker_user_id: int, taker_side: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        direction = Decimal("1") if taker_side == "buy" else Decimal("-1")
        for fill in fills:
            quantity = Decimal(fill.quantity)
            if self.product_type == "PERP" and taker_user_id:
                self.positions[taker_user_id] = self.positions.get(taker_user_id, Decimal("0")) + direction * quantity
            result.append(
                {
                    "maker_order_id": fill.maker_order_id,
                    "maker_user_id": int(fill.maker_user_id),
                    "taker_user_id": int(taker_user_id),
                    "price": str(fill.price),
                    "quantity": str(fill.quantity),
                }
            )
        return result

    def _validate_invariants(self) -> None:
        try:
            book = self.engine.ensure_market(self.market_id)
            book.validate_invariants()
            if not set(self.order_index).issubset(set(book.orders)):
                raise BookInvariantError("order metadata points outside the engine book")
        except Exception:
            self.metrics["invariant_failures"] += 1
            raise

    def update_reference(self, envelope: IPCEnvelope) -> bool:
        envelope.validate(expected_run_id=self.run_id)
        if envelope.market_id != self.market_id:
            return False
        timestamp = int(envelope.payload.get("source_timestamp") or envelope.sent_at)
        if timestamp < int(self.reference_state.get("source_timestamp") or 0):
            return False
        self.reference_state = {
            "bid": envelope.payload.get("bid"),
            "ask": envelope.payload.get("ask"),
            "mid": envelope.payload.get("mid"),
            "source_timestamp": timestamp,
            "received_at": now_ms(),
            "source": str(envelope.payload.get("source") or "binance"),
        }
        return True

    def reference_status(self, *, stale_after_ms: int) -> dict[str, Any]:
        source_timestamp = int(self.reference_state.get("source_timestamp") or 0)
        received = int(self.reference_state.get("received_at") or 0)
        # A frame that sat in a queue must not become fresh merely because the
        # worker drained it just now.  Prefer exchange/source time; received_at
        # is only a fallback for sources that cannot provide one.
        freshness_timestamp = source_timestamp if source_timestamp > 0 else received
        age = 10**9 if freshness_timestamp <= 0 else max(0, now_ms() - freshness_timestamp)
        state = dict(self.reference_state)
        state.update({"age_ms": age, "status": "stale" if age > stale_after_ms else "fresh"})
        return state

    def queue_lag_p99(self) -> float:
        if not self.queue_lag_samples:
            return 0.0
        ordered = sorted(self.queue_lag_samples)
        index = min(len(ordered) - 1, round(0.99 * (len(ordered) - 1)))
        return round(float(ordered[index]), 3)

    def book_change_rate(self, *, window_ms: int = 10_000) -> float:
        timestamp = now_ms()
        while self.book_progress_times and timestamp - self.book_progress_times[0] > window_ms:
            self.book_progress_times.popleft()
        return round(len(self.book_progress_times) * 1_000 / max(1, window_ms), 3)

    @staticmethod
    def _changes(previous: list[list[str]], current: list[list[str]]) -> tuple[tuple[str, str], ...]:
        old = {str(price): str(quantity) for price, quantity in previous}
        new = {str(price): str(quantity) for price, quantity in current}
        changes = [(price, quantity) for price, quantity in new.items() if old.get(price) != quantity]
        changes.extend((price, "0") for price in old if price not in new)
        return tuple(changes)

    def published_event(self, *, force_snapshot: bool = False) -> PublishedBookEvent | None:
        if self.engine.fault.halted:
            return None
        snapshot = self.engine.snapshot(self.market_id)
        if not force_snapshot and snapshot == self._last_book:
            return None
        previous_seq = self.publish_seq
        if snapshot != self._last_book:
            self.publish_seq += 1
        is_snapshot = bool(force_snapshot or previous_seq == 0)
        if is_snapshot:
            bids = tuple((str(price), str(quantity)) for price, quantity in snapshot.get("bids", []))
            asks = tuple((str(price), str(quantity)) for price, quantity in snapshot.get("asks", []))
        else:
            bids = self._changes(self._last_book.get("bids", []), snapshot.get("bids", []))
            asks = self._changes(self._last_book.get("asks", []), snapshot.get("asks", []))
        self._last_book = {
            "bids": [list(item) for item in snapshot.get("bids", [])],
            "asks": [list(item) for item in snapshot.get("asks", [])],
        }
        published_at = now_ms()
        self.last_book_progress_at = published_at
        self.book_progress_times.append(published_at)
        return PublishedBookEvent(
            run_id=self.run_id,
            market_id=self.market_id,
            stream_epoch=self.stream_epoch,
            stream_id=self.stream_id,
            seq=self.publish_seq,
            previous_seq=previous_seq,
            published_at=published_at,
            snapshot=is_snapshot,
            bids=bids,
            asks=asks,
            engine_version=self.engine.market_version(self.market_id),
        )

    def full_snapshot_event(self) -> PublishedBookEvent:
        event = self.published_event(force_snapshot=True)
        assert event is not None
        return event


def _reliable_put(queue: Any, event: ReliableWorkerEvent, stop_event: Any) -> bool:
    while not stop_event.is_set():
        try:
            queue.put(event, block=True, timeout=0.1)
            return True
        except Full:
            # Reliable ACK/private/lifecycle events are never discarded.  A
            # lost gateway safely pauses the worker at this bounded edge.
            continue
    return False


def _publish_book(runtime: EngineWorkerRuntime, book_queue: Any, *, force_snapshot: bool = False) -> None:
    event = runtime.published_event(force_snapshot=force_snapshot)
    if event is None:
        return
    dropped = put_latest(book_queue, event)
    if not dropped:
        return
    runtime.metrics["book_frames_dropped"] += dropped
    # Once a delta was coalesced, publish a full state so the Gateway can
    # converge without depending on an intermediate frame.  This second
    # latest-only put is also time-bounded and can never pause the worker.
    runtime.metrics["book_frames_dropped"] += put_latest(
        book_queue,
        runtime.full_snapshot_event(),
    )


def engine_worker_main(
    config: dict[str, Any],
    command_queue: Any,
    plan_queue: Any,
    reference_queue: Any,
    control_queue: Any,
    reliable_queue: Any,
    book_queue: Any,
    metrics_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    runtime = EngineWorkerRuntime(
        run_id=str(config["run_id"]),
        market_id=str(config["market_id"]),
        product_type=str(config["product_type"]),
    )
    stale_after_ms = max(50, int(config.get("reference_stale_ms", 1_000)))
    ready = {
        "component": str(config["component"]),
        "pid": os.getpid(),
        "market_id": runtime.market_id,
        "stream_epoch": runtime.stream_epoch,
        "stream_id": runtime.stream_id,
        "status": "READY",
        "at": now_ms(),
    }
    ready_queue.put(ready)
    _reliable_put(
        reliable_queue,
        ReliableWorkerEvent(
            run_id=runtime.run_id,
            market_id=runtime.market_id,
            stream_epoch=runtime.stream_epoch,
            event_type="WORKER_READY",
            command_id="",
            request_fingerprint="",
            payload=ready,
        ),
        stop_event,
    )
    _publish_book(runtime, book_queue, force_snapshot=True)
    last_heartbeat = 0

    while not stop_event.is_set():
        did_work = False
        latest_reference, _dropped = drain_latest(reference_queue)
        if isinstance(latest_reference, IPCEnvelope):
            runtime.update_reference(latest_reference)
            did_work = True

        try:
            control = control_queue.get_nowait()
        except Empty:
            control = None
        if isinstance(control, IPCEnvelope):
            result = runtime.handle(control)
            _reliable_put(
                reliable_queue,
                ReliableWorkerEvent(
                    run_id=runtime.run_id,
                    market_id=runtime.market_id,
                    stream_epoch=runtime.stream_epoch,
                    event_type="COMMAND_RESULT",
                    command_id=control.command_id,
                    request_fingerprint=control.request_fingerprint,
                    payload=result,
                ),
                stop_event,
            )
            if result.get("force_snapshot"):
                _publish_book(runtime, book_queue, force_snapshot=True)
            did_work = True

        try:
            command = command_queue.get_nowait()
        except Empty:
            command = None
        if isinstance(command, IPCEnvelope):
            result = runtime.handle(command)
            _publish_book(runtime, book_queue)
            _reliable_put(
                reliable_queue,
                ReliableWorkerEvent(
                    run_id=runtime.run_id,
                    market_id=runtime.market_id,
                    stream_epoch=runtime.stream_epoch,
                    event_type="COMMAND_RESULT",
                    command_id=command.command_id,
                    request_fingerprint=command.request_fingerprint,
                    payload=result,
                ),
                stop_event,
            )
            did_work = True

        latest_plan, superseded = drain_latest(plan_queue)
        if superseded:
            runtime.metrics["maker_plan_dropped"] += superseded
        if isinstance(latest_plan, IPCEnvelope):
            result = runtime.handle(latest_plan)
            _publish_book(runtime, book_queue)
            _reliable_put(
                reliable_queue,
                ReliableWorkerEvent(
                    run_id=runtime.run_id,
                    market_id=runtime.market_id,
                    stream_epoch=runtime.stream_epoch,
                    event_type="COMMAND_RESULT",
                    command_id=latest_plan.command_id,
                    request_fingerprint=latest_plan.request_fingerprint,
                    payload=result,
                ),
                stop_event,
            )
            did_work = True

        timestamp = now_ms()
        if timestamp - last_heartbeat >= 500:
            last_heartbeat = timestamp
            heartbeat = {
                "component": str(config["component"]),
                "pid": os.getpid(),
                "market_id": runtime.market_id,
                "stream_epoch": runtime.stream_epoch,
                "at": timestamp,
                "worker_lag_ms": float(runtime.metrics["queue_lag_ms_last"]),
                "worker_lag_p99_ms": runtime.queue_lag_p99(),
                "reference": runtime.reference_status(stale_after_ms=stale_after_ms),
                "queues": {
                    "commands": _queue_size(command_queue),
                    "plans": _queue_size(plan_queue),
                    "references": _queue_size(reference_queue),
                    "control": _queue_size(control_queue),
                },
                "runtime": {**runtime.metrics, "matching_gate": runtime.engine.fault.snapshot()},
                "book_seq": runtime.publish_seq,
                "stream_id": runtime.stream_id,
                "last_book_progress_at": runtime.last_book_progress_at,
                "book_change_rate": runtime.book_change_rate(),
                "open_orders": len(runtime.engine.ensure_market(runtime.market_id).orders),
            }
            try:
                metrics_queue.put_nowait(heartbeat)
            except Full:
                pass
        if not did_work:
            time.sleep(0.001)

    _reliable_put(
        reliable_queue,
        ReliableWorkerEvent(
            run_id=runtime.run_id,
            market_id=runtime.market_id,
            stream_epoch=runtime.stream_epoch,
            event_type="WORKER_STOPPED",
            command_id="",
            request_fingerprint="",
            payload={"pid": os.getpid(), "at": now_ms()},
        ),
        stop_event,
    )
