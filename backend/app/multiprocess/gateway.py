from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import json
import os
from pathlib import Path
from queue import Empty, Full
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

from app.multiprocess.protocol import (
    IPCEnvelope,
    PublishedBookEvent,
    ReliableWorkerEvent,
    canonical_fingerprint,
    now_ms,
    put_latest,
)
from app.multiprocess.alerts import AlertAggregator


MARKETS = ("BTCUSDT", "BTCUSDT-PERP")
MARKET_BY_COMPONENT = {"spot_engine": "BTCUSDT", "perp_engine": "BTCUSDT-PERP"}


def _queue_size(queue: Any) -> int:
    try:
        return max(0, int(queue.qsize()))
    except (AttributeError, NotImplementedError, OSError):
        return -1


@dataclass(frozen=True, slots=True)
class ImmutableBook:
    market_id: str
    stream_epoch: str
    stream_id: str
    seq: int
    published_at: int
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    engine_version: int

    def as_dict(self, *, frame_type: str = "snapshot") -> dict[str, Any]:
        return {
            "channel": "orderbook",
            "type": frame_type,
            "market_id": self.market_id,
            "symbol": self.market_id,
            "stream_epoch": self.stream_epoch,
            "stream_id": self.stream_id,
            "seq": self.seq,
            "published_at": self.published_at,
            "bids": [list(item) for item in self.bids],
            "asks": [list(item) for item in self.asks],
            "engine_version": self.engine_version,
        }


@dataclass(slots=True)
class CommandRecord:
    command_id: str
    request_fingerprint: str
    market_id: str
    stream_epoch: str
    kind: str
    status: str
    accepted_at: int
    deadline: int
    result: dict[str, Any]
    future: asyncio.Future[dict[str, Any]] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "request_fingerprint": self.request_fingerprint,
            "market_id": self.market_id,
            "stream_epoch": self.stream_epoch,
            "kind": self.kind,
            "status": self.status,
            "accepted_at": self.accepted_at,
            "deadline": self.deadline,
            **self.result,
        }


@dataclass(eq=False, slots=True)
class SocketClient:
    websocket: WebSocket
    market_id: str | None
    user_id: int | None
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=16))


class GatewayCommandRequest(BaseModel):
    kind: str = Field(pattern="^[A-Za-z_]+$")
    payload: dict[str, Any] = Field(default_factory=dict)
    command_id: str | None = None
    deadline_ms: int = Field(default=2_000, ge=1, le=60_000)
    wait_ms: int = Field(default=2_000, ge=0, le=60_000)
    priority_sequence: int | None = Field(default=None, ge=0)


class RuntimeActionRequest(BaseModel):
    action: str = Field(pattern="^[A-Za-z_]+$")
    target: str = ""
    profile_ms: int | None = Field(default=None, ge=1, le=10_000)
    impact: str = ""


class GatewayState:
    def __init__(self, config: dict[str, Any], queues: dict[str, Any]) -> None:
        self.config = config
        self.run_id = str(config["run_id"])
        self.api_key = str(config.get("api_key") or "mp-sandbox-key")
        self.command_queues: dict[str, Any] = queues["command_queues"]
        self.plan_queues: dict[str, Any] = queues["plan_queues"]
        self.control_queues: dict[str, Any] = queues["control_queues"]
        self.reliable_queues: dict[str, Any] = queues["reliable_queues"]
        self.book_queues: dict[str, Any] = queues["book_queues"]
        self.metrics_output_queue = queues["metrics_output_queue"]
        self.metrics_ingress_queue = queues["metrics_ingress_queue"]
        self.supervisor_control_queue = queues.get("supervisor_control_queue")
        self.market_epochs: dict[str, str | None] = {market: None for market in MARKETS}
        self.stream_ids: dict[str, str | None] = {market: None for market in MARKETS}
        self.books: dict[str, ImmutableBook] = {}
        self.last_business_progress_at: dict[str, int | None] = {market: None for market in MARKETS}
        self.book_change_times: dict[str, deque[int]] = {market: deque(maxlen=2_048) for market in MARKETS}
        self.command_sequence = 0
        self.priority_sequence = 0
        self.command_records: OrderedDict[str, CommandRecord] = OrderedDict()
        self.client_fences: OrderedDict[tuple[str, int, str], tuple[str, str]] = OrderedDict()
        self.record_limit = max(256, int(config.get("command_cache_size", 20_000)))
        self.public_clients: set[SocketClient] = set()
        self.private_clients: dict[int, set[SocketClient]] = {}
        self.metrics_snapshot: dict[str, Any] = {}
        self.running = False
        self.tasks: list[asyncio.Task[Any]] = []
        self.uds_server: asyncio.AbstractServer | None = None
        self.started_at_ms = now_ms()
        self.alerts = AlertAggregator(window_ms=60_000)
        self.action_records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.action_record_limit = 256
        self.maker_paused: dict[str, bool] = {market: False for market in MARKETS}
        self.thresholds = {
            "reference_stale_ms": max(50, int(config.get("reference_stale_ms", 1_000))),
            "book_stale_ms": max(500, int(config.get("book_stale_ms", 5_000))),
            "heartbeat_stale_ms": max(500, int(config.get("heartbeat_stale_ms", 2_500))),
        }
        self.counters = {
            "stale_worker_messages": 0,
            "book_gaps": 0,
            "snapshot_requests": 0,
            "socket_frames_dropped": 0,
            "command_queue_full": 0,
            "plan_frames_dropped": 0,
            "worker_restarts": 0,
            "run_resets": 0,
            "book_seq_rollbacks": 0,
            "book_same_seq_conflicts": 0,
            "book_stream_id_conflicts": 0,
            "book_invalid_frames": 0,
            "ws_send_timeouts": 0,
            "runtime_actions": 0,
        }

    async def start(self) -> None:
        self.running = True
        self.tasks = [
            *(
                asyncio.create_task(
                    self._consume_reliable(market),
                    name=f"mp-gateway-reliable-{market}",
                )
                for market in MARKETS
            ),
            asyncio.create_task(self._consume_metrics(), name="mp-gateway-metrics"),
            asyncio.create_task(self._heartbeat(), name="mp-gateway-heartbeat"),
            *(asyncio.create_task(self._consume_books(market), name=f"mp-gateway-book-{market}") for market in MARKETS),
        ]
        uds_path = Path(str(self.config["uds_path"]))
        uds_path.parent.mkdir(parents=True, exist_ok=True)
        if uds_path.exists():
            uds_path.unlink()
        self.uds_server = await asyncio.start_unix_server(self._handle_uds, path=str(uds_path))

    async def stop(self) -> None:
        self.running = False
        if self.uds_server is not None:
            self.uds_server.close()
            await self.uds_server.wait_closed()
            self.uds_server = None
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        uds_path = Path(str(self.config["uds_path"]))
        if uds_path.exists():
            uds_path.unlink()

    def _next_sequences(self, requested_priority: int | None = None) -> tuple[int, int]:
        self.command_sequence += 1
        self.priority_sequence += 1
        priority = self.priority_sequence if requested_priority is None else int(requested_priority)
        return self.command_sequence, priority

    def _record_alert(
        self,
        message: str,
        *,
        severity: str = "DEGRADED",
        priority: int = 1,
        code: str | None = None,
    ) -> None:
        self.alerts.record(message, now_ms=now_ms(), severity=severity, priority=priority, code=code)

    @staticmethod
    def _age(timestamp: Any, *, now: int | None = None) -> int | None:
        try:
            value = int(timestamp or 0)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return max(0, int(now or now_ms()) - value)

    def _book_rate(self, market: str, *, window_ms: int = 10_000) -> float:
        timestamp = now_ms()
        history = self.book_change_times[market]
        while history and timestamp - history[0] > window_ms:
            history.popleft()
        return round(len(history) * 1_000 / max(1, window_ms), 3)

    def _latest_heartbeat(self, component: str) -> dict[str, Any]:
        heartbeats = self.metrics_snapshot.get("heartbeats") or {}
        item = heartbeats.get(component)
        return dict(item) if isinstance(item, dict) else {}

    def _process_component(self, role: str) -> dict[str, Any]:
        process_tree = self.metrics_snapshot.get("process_tree") or {}
        components = process_tree.get("components") or {}
        item = components.get(role)
        if isinstance(item, dict):
            return dict(item)
        return {"pid": None, "alive": False, "status": "missing"}

    def _worker_health(self, component: str, market: str) -> dict[str, Any]:
        heartbeat = self._latest_heartbeat(component)
        reference = heartbeat.get("reference") if isinstance(heartbeat.get("reference"), dict) else {}
        heartbeat_age = self._age(heartbeat.get("at"))
        observed_reference_age = reference.get("age_ms")
        reference_age = int(observed_reference_age) if observed_reference_age is not None else self._age(reference.get("source_timestamp") or reference.get("received_at"))
        book = self.books.get(market)
        business_at = self.last_business_progress_at.get(market)
        business_age = self._age(business_at)
        engine_status = "HEALTHY"
        reasons: list[str] = []
        process = self._process_component(component)
        if not process.get("alive", False) or heartbeat_age is None or heartbeat_age > self.thresholds["heartbeat_stale_ms"]:
            engine_status = "HALTED"
            reasons.append("worker heartbeat stale or process not alive")
        worker_status = engine_status
        if str(reference.get("status") or "missing").lower() != "fresh" or reference_age is None or reference_age > self.thresholds["reference_stale_ms"]:
            worker_status = "DEGRADED" if worker_status != "HALTED" else worker_status
            reasons.append("Binance reference stale")
        return {
            "status": worker_status,
            "worker_status": worker_status,
            "engine_status": engine_status,
            "pid": process.get("pid") or heartbeat.get("pid"),
            "restart_count": int((self.metrics_snapshot.get("worker_restarts") or {}).get(component, 0)),
            "heartbeat_age_ms": heartbeat_age,
            "reference": reference,
            "binance_age_ms": reference_age,
            "binance_age_at_heartbeat_ms": reference_age,
            "reference_observed_at": heartbeat.get("at"),
            "command_queue": (heartbeat.get("queues") or {}).get("commands", -1),
            "plan_queue": (heartbeat.get("queues") or {}).get("plans", -1),
            "command_age_ms": heartbeat.get("worker_lag_ms"),
            "book_seq": None if book is None else book.seq,
            "stream_epoch": self.market_epochs.get(market),
            "stream_id": self.stream_ids.get(market) or (None if book is None else book.stream_id),
            "last_business_progress_at": business_at,
            "book_age_ms": business_age,
            "measured_refresh_rate": self._book_rate(market),
            "reasons": reasons,
        }

    def _store_record(self, record: CommandRecord) -> None:
        self.command_records[record.command_id] = record
        self.command_records.move_to_end(record.command_id)
        while len(self.command_records) > self.record_limit:
            key, candidate = next(iter(self.command_records.items()))
            if candidate.status in {"ACCEPTED", "PENDING"}:
                self.command_records.move_to_end(key)
                if all(item.status in {"ACCEPTED", "PENDING"} for item in self.command_records.values()):
                    break
                continue
            self.command_records.popitem(last=False)
        while len(self.client_fences) > self.record_limit:
            self.client_fences.popitem(last=False)

    async def submit(
        self,
        market_id: str,
        request: GatewayCommandRequest,
        *,
        maker_plan: bool = False,
    ) -> dict[str, Any]:
        market = str(market_id).upper()
        if market not in self.command_queues:
            return {"status": "MARKET_NOT_FOUND", "market_id": market}
        kind = "MAKER_PLAN" if maker_plan else str(request.kind).upper()
        payload = dict(request.payload)
        command_id = str(request.command_id or uuid4())
        fingerprint = canonical_fingerprint(market_id=market, kind=kind, payload=payload)
        existing = self.command_records.get(command_id)
        if existing is not None:
            self.command_records.move_to_end(command_id)
            if existing.request_fingerprint != fingerprint:
                return {
                    "status": "IDEMPOTENCY_CONFLICT",
                    "command_id": command_id,
                    "request_fingerprint": fingerprint,
                    "original_fingerprint": existing.request_fingerprint,
                }
            return existing.public()

        user_id = int(payload.get("user_id") or 0)
        client_order_id = str(payload.get("client_order_id") or "")
        fence_key = (market, user_id, client_order_id)
        if kind == "PLACE" and client_order_id and fence_key in self.client_fences:
            original_command_id, original_fingerprint = self.client_fences[fence_key]
            original = self.command_records.get(original_command_id)
            if original_fingerprint != fingerprint:
                return {
                    "status": "CLIENT_ORDER_ID_CONFLICT",
                    "command_id": command_id,
                    "original_command_id": original_command_id,
                    "request_fingerprint": fingerprint,
                }
            if original is not None:
                return original.public()

        sequence, priority = self._next_sequences(request.priority_sequence)
        timestamp = now_ms()
        deadline = timestamp + int(request.deadline_ms)
        epoch = str(self.market_epochs.get(market) or "*")
        envelope = IPCEnvelope.build(
            run_id=self.run_id,
            market_id=market,
            stream_epoch=epoch,
            kind=kind,
            payload=payload,
            command_id=command_id,
            command_sequence=sequence,
            priority_sequence=priority,
            sent_at=timestamp,
            deadline=deadline,
        )
        loop = asyncio.get_running_loop()
        record = CommandRecord(
            command_id=command_id,
            request_fingerprint=fingerprint,
            market_id=market,
            stream_epoch=epoch,
            kind=kind,
            status="ACCEPTED",
            accepted_at=timestamp,
            deadline=deadline,
            result={"status": "ACCEPTED"},
            future=loop.create_future(),
        )
        self._store_record(record)
        if kind == "PLACE" and client_order_id:
            self.client_fences[fence_key] = (command_id, fingerprint)
            self.client_fences.move_to_end(fence_key)
        if maker_plan:
            dropped = put_latest(self.plan_queues[market], envelope)
            self.counters["plan_frames_dropped"] += dropped
        else:
            try:
                self.command_queues[market].put_nowait(envelope)
            except Full:
                self.counters["command_queue_full"] += 1
                record.status = "REJECTED_QUEUE_FULL"
                record.result = {
                    "status": "REJECTED_QUEUE_FULL",
                    "reason": "bounded user command queue is full; command was not enqueued",
                }
                if record.future is not None and not record.future.done():
                    record.future.set_result(record.public())
                return record.public()
        record.status = "PENDING"
        record.result = {"status": "PENDING"}
        if request.wait_ms <= 0 or record.future is None:
            return record.public()
        try:
            return await asyncio.wait_for(asyncio.shield(record.future), timeout=request.wait_ms / 1_000)
        except asyncio.TimeoutError:
            if record.future.done():
                return record.future.result()
            # The future remains live.  An eventual ACK updates status, but the
            # gateway never re-enqueues this unknown-state trading command.
            record.status = "UNKNOWN_TIMEOUT"
            record.result = {
                "status": "UNKNOWN_TIMEOUT",
                "reason": "command outcome is unknown; query command status and do not auto-retry",
            }
            return record.public()

    async def _consume_reliable(self, market_id: str) -> None:
        queue = self.reliable_queues[market_id]
        while self.running:
            try:
                event = queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.001)
                continue
            if isinstance(event, ReliableWorkerEvent):
                await self.handle_reliable(event)

    async def handle_reliable(self, event: ReliableWorkerEvent) -> None:
        if event.run_id != self.run_id or event.market_id not in self.market_epochs:
            self.counters["stale_worker_messages"] += 1
            return
        if event.event_type == "WORKER_READY":
            previous = self.market_epochs.get(event.market_id)
            self.market_epochs[event.market_id] = event.stream_epoch
            announced_stream_id = str(event.payload.get("stream_id") or "")
            self.stream_ids[event.market_id] = announced_stream_id or None
            epoch_changed = previous is not None and previous != event.stream_epoch
            if epoch_changed:
                self.counters["worker_restarts"] += 1
                self.counters["run_resets"] += 1
                for record in self.command_records.values():
                    if record.market_id != event.market_id or record.status not in {"ACCEPTED", "PENDING", "UNKNOWN_TIMEOUT"}:
                        continue
                    record.status = "UNKNOWN_WORKER_RESTARTED"
                    record.result = {
                        "status": "UNKNOWN_WORKER_RESTARTED",
                        "reason": "worker exited before a final ACK; sampled runtime was reset",
                        "old_stream_epoch": previous,
                        "new_stream_epoch": event.stream_epoch,
                        "run_reset": True,
                    }
                    if record.future is not None and not record.future.done():
                        record.future.set_result(record.public())
                # A new epoch is a new stream. Discard the prior immutable
                # book so a lower sequence from the new worker is legal.
                self.books.pop(event.market_id, None)
                self.last_business_progress_at[event.market_id] = None
                self.book_change_times[event.market_id].clear()
                await self._fanout_public(
                    {
                        "channel": "runtime",
                        "type": "run_reset",
                        "market_id": event.market_id,
                        "old_stream_epoch": previous,
                        "new_stream_epoch": event.stream_epoch,
                    }
                )
                # Reliable WORKER_READY and latest-only book frames travel
                # on separate queues.  The new worker's first snapshot can
                # therefore arrive before this fence and be rejected as an
                # old epoch.  Ask the now-authoritative worker for a second
                # full snapshot after the fence so Gateway cannot remain
                # without a valid empty/new book.
                await self._request_snapshot(event.market_id)
            self._record_alert(
                f"{event.market_id} worker ready epoch={event.stream_epoch}",
                severity="INFO",
                priority=0,
                code="worker_ready",
            )
            return
        current_epoch = self.market_epochs.get(event.market_id)
        if current_epoch is not None and event.stream_epoch != current_epoch:
            self.counters["stale_worker_messages"] += 1
            return
        if current_epoch is None:
            self.market_epochs[event.market_id] = event.stream_epoch
        if event.event_type != "COMMAND_RESULT":
            return
        record = self.command_records.get(event.command_id)
        if record is None:
            return
        if record.request_fingerprint != event.request_fingerprint:
            self.counters["stale_worker_messages"] += 1
            return
        record.stream_epoch = event.stream_epoch
        record.status = str(event.payload.get("status") or "ACKED")
        record.result = dict(event.payload)
        public = record.public()
        if record.future is not None and not record.future.done():
            record.future.set_result(public)
        for private in event.payload.get("private_events") or []:
            if isinstance(private, dict) and private.get("user_id") is not None:
                await self._fanout_private(int(private["user_id"]), private)
        try:
            self.metrics_ingress_queue.put_nowait(
                {
                    "component": "gateway",
                    "at": now_ms(),
                    "ipc_latency_ms": float(event.payload.get("ipc_latency_ms") or 0),
                }
            )
        except Full:
            pass

    async def _consume_books(self, market_id: str) -> None:
        queue = self.book_queues[market_id]
        while self.running:
            try:
                event = queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.001)
                continue
            if isinstance(event, PublishedBookEvent):
                await self.handle_book(event)

    async def handle_book(self, event: PublishedBookEvent) -> None:
        if (
            event.run_id != self.run_id
            or event.market_id not in self.market_epochs
            or not str(event.stream_epoch).strip()
            or not str(event.stream_id).strip()
            or int(event.seq) < 0
        ):
            self.counters["book_invalid_frames"] += 1
            self.counters["stale_worker_messages"] += 1
            return
        epoch = self.market_epochs.get(event.market_id)
        if epoch is not None and event.stream_epoch != epoch:
            self.counters["stale_worker_messages"] += 1
            return
        if epoch is None:
            self.market_epochs[event.market_id] = event.stream_epoch
        announced_stream_id = self.stream_ids.get(event.market_id)
        if announced_stream_id is not None and event.stream_id != announced_stream_id:
            self.counters["book_stream_id_conflicts"] += 1
            self._record_alert(
                f"{event.market_id} book stream_id conflict",
                severity="HALTED",
                priority=3,
                code="book_stream_id_conflict",
            )
            return
        self.stream_ids[event.market_id] = event.stream_id
        previous = self.books.get(event.market_id)
        if event.snapshot:
            if previous is not None and previous.stream_epoch == event.stream_epoch:
                if event.seq < previous.seq:
                    self.counters["book_seq_rollbacks"] += 1
                    self._record_alert(
                        f"{event.market_id} snapshot seq rollback {event.seq}<{previous.seq}",
                        severity="HALTED",
                        priority=3,
                        code="book_seq_rollback",
                    )
                    return
                if event.seq == previous.seq:
                    same_content = (
                        previous.stream_id == event.stream_id
                        and previous.bids == event.bids
                        and previous.asks == event.asks
                        and previous.engine_version == event.engine_version
                    )
                    if not same_content:
                        self.counters["book_same_seq_conflicts"] += 1
                        self._record_alert(
                            f"{event.market_id} same seq different content seq={event.seq}",
                            severity="HALTED",
                            priority=3,
                            code="book_same_seq_conflict",
                        )
                    return
            book = ImmutableBook(
                market_id=event.market_id,
                stream_epoch=event.stream_epoch,
                stream_id=event.stream_id,
                seq=event.seq,
                published_at=event.published_at,
                bids=event.bids,
                asks=event.asks,
                engine_version=event.engine_version,
            )
            frame_type = "snapshot"
        else:
            if (
                previous is None
                or previous.stream_epoch != event.stream_epoch
                or previous.stream_id != event.stream_id
                or event.previous_seq != previous.seq
                or event.seq != previous.seq + 1
            ):
                if previous is not None and event.stream_epoch == previous.stream_epoch and event.seq <= previous.seq:
                    self.counters["book_seq_rollbacks"] += 1
                    self._record_alert(
                        f"{event.market_id} delta seq rollback {event.seq}<={previous.seq}",
                        severity="HALTED",
                        priority=3,
                        code="book_seq_rollback",
                    )
                elif previous is not None and event.seq == previous.seq:
                    self.counters["book_same_seq_conflicts"] += 1
                self.counters["book_gaps"] += 1
                await self._request_snapshot(event.market_id)
                return
            bids = self._apply_levels(previous.bids, event.bids, reverse=True)
            asks = self._apply_levels(previous.asks, event.asks, reverse=False)
            book = ImmutableBook(
                market_id=event.market_id,
                stream_epoch=event.stream_epoch,
                stream_id=event.stream_id,
                seq=event.seq,
                published_at=event.published_at,
                bids=bids,
                asks=asks,
                engine_version=event.engine_version,
            )
            frame_type = "delta"
        self.books[event.market_id] = book
        self.last_business_progress_at[event.market_id] = int(event.published_at)
        self.book_change_times[event.market_id].append(int(event.published_at))
        await self._fanout_public(book.as_dict(frame_type=frame_type))
        if event.seq > 0:
            try:
                self.metrics_ingress_queue.put_nowait(
                    {
                        "component": "gateway",
                        "at": now_ms(),
                        "book_latency_ms": max(0, now_ms() - event.published_at),
                    }
                )
            except Full:
                pass

    @staticmethod
    def _apply_levels(
        current: tuple[tuple[str, str], ...],
        changes: tuple[tuple[str, str], ...],
        *,
        reverse: bool,
    ) -> tuple[tuple[str, str], ...]:
        levels = dict(current)
        for price, quantity in changes:
            if Decimal(str(quantity)) == 0:
                levels.pop(str(price), None)
            else:
                levels[str(price)] = str(quantity)
        return tuple(sorted(levels.items(), key=lambda item: Decimal(item[0]), reverse=reverse))

    async def _request_snapshot(self, market_id: str) -> None:
        sequence, priority = self._next_sequences()
        timestamp = now_ms()
        envelope = IPCEnvelope.build(
            run_id=self.run_id,
            market_id=market_id,
            stream_epoch=str(self.market_epochs.get(market_id) or "*"),
            kind="SNAPSHOT_REQUEST",
            payload={"reason": "gateway_delta_gap"},
            command_sequence=sequence,
            priority_sequence=priority,
            sent_at=timestamp,
            deadline=timestamp + 1_000,
        )
        try:
            self.control_queues[market_id].put_nowait(envelope)
            self.counters["snapshot_requests"] += 1
        except Full:
            pass

    async def _consume_metrics(self) -> None:
        while self.running:
            try:
                item = self.metrics_output_queue.get_nowait()
            except Empty:
                await asyncio.sleep(0.01)
                continue
            if isinstance(item, dict) and item.get("run_id") == self.run_id:
                self.metrics_snapshot = item

    async def _heartbeat(self) -> None:
        while self.running:
            for component, market in MARKET_BY_COMPONENT.items():
                health = self._worker_health(component, market)
                should_pause = health["engine_status"] != "HEALTHY" or health["reference"].get("status") != "fresh" or health["binance_age_ms"] is None or health["binance_age_ms"] > self.thresholds["reference_stale_ms"]
                if should_pause != self.maker_paused[market]:
                    sequence, priority = self._next_sequences()
                    timestamp = now_ms()
                    envelope = IPCEnvelope.build(
                        run_id=self.run_id,
                        market_id=market,
                        stream_epoch=str(self.market_epochs.get(market) or "*"),
                        kind="PAUSE_MAKER" if should_pause else "RESUME_MAKER",
                        payload={"reason": "runtime_readiness_gate"},
                        command_sequence=sequence,
                        priority_sequence=priority,
                        sent_at=timestamp,
                        deadline=timestamp + 1_000,
                    )
                    try:
                        self.control_queues[market].put_nowait(envelope)
                        self.maker_paused[market] = should_pause
                    except Full:
                        pass
            try:
                self.metrics_ingress_queue.put_nowait(
                    {
                        "component": "gateway",
                        "pid": os.getpid(),
                        "at": now_ms(),
                        "queues": {
                            **{
                                f"reliable:{market}": _queue_size(queue)
                                for market, queue in self.reliable_queues.items()
                            },
                            **{f"commands:{market}": _queue_size(queue) for market, queue in self.command_queues.items()},
                            **{f"book:{market}": _queue_size(queue) for market, queue in self.book_queues.items()},
                        },
                        "book_progress": {
                            market: {
                                "seq": None if self.books.get(market) is None else self.books[market].seq,
                                "stream_epoch": self.market_epochs.get(market),
                                "stream_id": self.stream_ids.get(market),
                                "last_business_progress_at": self.last_business_progress_at.get(market),
                                "measured_refresh_rate": self._book_rate(market),
                            }
                            for market in MARKETS
                        },
                        "ws": {
                            "public_clients": len(self.public_clients),
                            "private_clients": sum(len(clients) for clients in self.private_clients.values()),
                            "send_timeouts": self.counters["ws_send_timeouts"],
                            "drops": self.counters["socket_frames_dropped"],
                        },
                        "counters": dict(self.counters),
                    }
                )
            except Full:
                pass
            await asyncio.sleep(1.0)

    async def _fanout_public(self, payload: dict[str, Any]) -> None:
        market = str(payload.get("market_id") or "")
        for client in list(self.public_clients):
            if client.market_id and market and client.market_id != market:
                continue
            if client.queue.full():
                try:
                    client.queue.get_nowait()
                    client.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                self.counters["socket_frames_dropped"] += 1
                snapshot = self.books.get(market)
                payload_to_send = snapshot.as_dict() if snapshot is not None else payload
            else:
                payload_to_send = payload
            try:
                client.queue.put_nowait(payload_to_send)
            except asyncio.QueueFull:
                self.counters["socket_frames_dropped"] += 1

    async def _fanout_private(self, user_id: int, payload: dict[str, Any]) -> None:
        for client in list(self.private_clients.get(user_id, set())):
            if client.queue.full():
                try:
                    client.queue.get_nowait()
                    client.queue.task_done()
                except asyncio.QueueEmpty:
                    pass
                self.counters["socket_frames_dropped"] += 1
            try:
                client.queue.put_nowait({"channel": "private", **payload})
            except asyncio.QueueFull:
                self.counters["socket_frames_dropped"] += 1

    def status(self) -> dict[str, Any]:
        timestamp = now_ms()
        engine_health = {
            component: self._worker_health(component, market)
            for component, market in MARKET_BY_COMPONENT.items()
        }
        market_data_health = {
            market: {
                "status": "HEALTHY" if item["reference"].get("status") == "fresh" and item["binance_age_ms"] is not None and item["binance_age_ms"] <= self.thresholds["reference_stale_ms"] else "STALE",
                "source": item["reference"].get("source", "missing"),
                "price": item["reference"].get("mid"),
                "bid": item["reference"].get("bid"),
                "ask": item["reference"].get("ask"),
                "updated_at": item["reference"].get("source_timestamp"),
                "binance_age_ms": item["binance_age_ms"],
                "reason": "" if item["reference"].get("status") == "fresh" and item["binance_age_ms"] is not None and item["binance_age_ms"] <= self.thresholds["reference_stale_ms"] else "Binance reference stale",
            }
            for market, item in ((market, engine_health[component]) for component, market in MARKET_BY_COMPONENT.items())
        }
        book_freshness = {
            market: {
                "status": "FRESH" if item["book_age_ms"] is not None and item["book_age_ms"] <= self.thresholds["book_stale_ms"] else "STALE",
                "seq": item["book_seq"],
                "stream_epoch": item["stream_epoch"],
                "stream_id": item["stream_id"],
                "last_business_progress_at": item["last_business_progress_at"],
                "age_ms": item["book_age_ms"],
                "measured_refresh_rate": item["measured_refresh_rate"],
                "heartbeat_is_not_progress": True,
            }
            for market, item in ((market, engine_health[component]) for component, market in MARKET_BY_COMPONENT.items())
        }
        process_tree = self.metrics_snapshot.get("process_tree") or {}
        required_processes = {"supervisor": self._process_component("supervisor"), "gateway": self._process_component("gateway"), "feed": self._process_component("feed"), "spot_engine": self._process_component("spot_engine"), "perp_engine": self._process_component("perp_engine"), "sampler": self._process_component("sampler"), "metrics": self._process_component("metrics")}
        liveness_failures = [role for role, item in required_processes.items() if item.get("pid") and not item.get("alive", False)]
        gateway_heartbeat = self._latest_heartbeat("gateway")
        gateway_heartbeat_age = self._age(gateway_heartbeat.get("at"))
        sampler_heartbeat = self._latest_heartbeat("sampler")
        sampler_age = self._age(sampler_heartbeat.get("at"))
        sampler_storage = sampler_heartbeat.get("storage") if isinstance(sampler_heartbeat.get("storage"), dict) else {}
        if not sampler_storage:
            sampler_storage = self.metrics_snapshot.get("sampler_storage") if isinstance(self.metrics_snapshot.get("sampler_storage"), dict) else {}
        storage_usage = sampler_storage.get("usage") if isinstance(sampler_storage.get("usage"), dict) else {}
        storage_status_raw = str(sampler_storage.get("status") or "unknown")
        storage_status = "HEALTHY" if storage_status_raw in {"running", "healthy"} else storage_status_raw.upper()
        sampler_status = "HEALTHY" if sampler_age is not None and sampler_age <= self.thresholds["heartbeat_stale_ms"] else "DEGRADED"
        if storage_status in {"STORAGE_DEGRADED", "DEGRADED"}:
            sampler_status = "DEGRADED"

        readiness_reasons: list[dict[str, Any]] = []
        for component, item in engine_health.items():
            if item["engine_status"] != "HEALTHY":
                readiness_reasons.append({"code": "engine_unavailable", "component": component, "detail": "engine heartbeat/process is not healthy", "affects_readiness": True})
            if item["reference"].get("status") != "fresh" or item["binance_age_ms"] is None or item["binance_age_ms"] > self.thresholds["reference_stale_ms"]:
                readiness_reasons.append({"code": "binance_stale", "component": component, "detail": "Binance reference is stale", "affects_readiness": True})
        if gateway_heartbeat_age is None or gateway_heartbeat_age > self.thresholds["heartbeat_stale_ms"]:
            readiness_reasons.append({"code": "gateway_heartbeat_stale", "detail": "Gateway heartbeat is stale", "affects_readiness": True})
        degraded_reasons = list(readiness_reasons)
        if sampler_status != "HEALTHY":
            degraded_reasons.append({"code": "sampler_degraded", "detail": "history sampling/storage is degraded; matching is not halted", "affects_readiness": False})
        for market, item in book_freshness.items():
            if item["status"] == "STALE":
                degraded_reasons.append({"code": "book_stale", "market_id": market, "detail": "heartbeat may be alive while business book progress is stale", "affects_readiness": False})
        ws_heartbeat = gateway_heartbeat.get("ws") if isinstance(gateway_heartbeat.get("ws"), dict) else {}
        if int(ws_heartbeat.get("send_timeouts") or self.counters["ws_send_timeouts"]) or int(ws_heartbeat.get("drops") or self.counters["socket_frames_dropped"]):
            degraded_reasons.append({"code": "ws_degraded", "detail": "WebSocket send timeout/drop observed", "affects_readiness": False})

        engine_halted = any(item["engine_status"] == "HALTED" for item in engine_health.values())
        readiness_status = "READY" if not readiness_reasons else "NOT_READY"
        effective_status = "HALTED" if engine_halted or liveness_failures else ("DEGRADED" if degraded_reasons else "HEALTHY")
        if any(item["reference"].get("status") != "fresh" or item["binance_age_ms"] is None or item["binance_age_ms"] > self.thresholds["reference_stale_ms"] for item in engine_health.values()) and effective_status == "HEALTHY":
            effective_status = "DEGRADED"
        for reason in degraded_reasons:
            severity = "HALTED" if reason.get("affects_readiness") and reason.get("code") == "engine_unavailable" else "DEGRADED"
            self._record_alert(str(reason.get("detail")), severity=severity, priority=3 if severity == "HALTED" else 1, code=str(reason.get("code")))

        worker_status = {
            component: {
                "status": item["status"],
                "engine_status": item["engine_status"],
                "pid": item["pid"],
                "restart_count": item["restart_count"],
            }
            for component, item in engine_health.items()
        }
        workers_ready = all(self.market_epochs.values())
        books_ready = all(market in self.books for market in MARKETS)
        return {
            "ok": bool(workers_ready and books_ready and effective_status != "HALTED"),
            "status": effective_status,
            "effective_status": effective_status,
            "run_id": self.run_id,
            "gateway_pid": os.getpid(),
            "started_at": self.started_at_ms,
            "uptime_seconds": round(max(0, timestamp - self.started_at_ms) / 1_000, 3),
            "liveness": {"status": "ALIVE" if not liveness_failures else "FAILED", "failed_components": liveness_failures, "components": required_processes},
            "readiness": {"status": readiness_status, "reasons": readiness_reasons},
            "degraded_reasons": degraded_reasons,
            "market_data_health": market_data_health,
            "engine_health": {market: {**engine_health[component], "market_id": market} for component, market in MARKET_BY_COMPONENT.items()},
            "book_freshness": book_freshness,
            "sampler_health": {"status": sampler_status, "heartbeat_age_ms": sampler_age, "sampling_drops": int(sampler_heartbeat.get("input_drops") or 0) + int(sampler_heartbeat.get("writer_drops") or 0), "last_kline_finalized_time": sampler_heartbeat.get("last_kline_finalized_at")},
            "storage_health": {"status": storage_status, "raw_status": storage_status_raw, "mode": "sampled", "usage": storage_usage, "cap_bytes": sampler_storage.get("hard_limit_bytes") or sampler_storage.get("cap_bytes"), "soft_cap_bytes": sampler_storage.get("soft_limit_bytes"), "wal_cap_bytes": sampler_storage.get("wal_limit_bytes"), "path": self.config.get("history_path")},
            "ws_health": {"status": "DEGRADED" if self.counters["ws_send_timeouts"] or self.counters["socket_frames_dropped"] else "HEALTHY", "public_clients": len(self.public_clients), "private_clients": sum(len(clients) for clients in self.private_clients.values()), "send_timeout": self.counters["ws_send_timeouts"], "drop": self.counters["socket_frames_dropped"]},
            "worker_status": worker_status,
            "worker_restart_count": dict(self.metrics_snapshot.get("worker_restarts") or {}),
            "engine_command_queue": {component: item["command_queue"] for component, item in engine_health.items()},
            "command_age": {component: item["command_age_ms"] for component, item in engine_health.items()},
            "ack_cache": {"size": len(self.command_records), "capacity": self.record_limit, "pending": sum(record.status in {"ACCEPTED", "PENDING", "UNKNOWN_TIMEOUT"} for record in self.command_records.values())},
            "binance_age_ms": {market: item["binance_age_ms"] for market, item in ((market, engine_health[component]) for component, market in MARKET_BY_COMPONENT.items())},
            "kline_last_finalized_time": sampler_heartbeat.get("last_kline_finalized_at") or {},
            "refresh_intervals": {"ui_coalesce_ms": int(self.config.get("ui_coalesce_ms", 25)), "feed_ms": int(self.config.get("feed_interval_ms", 20)), "sampler_ms": int(self.config.get("sampler_interval_ms", 1_000)), "metrics_ms": 1_000, "book_stale_ms": self.thresholds["book_stale_ms"]},
            "market_epochs": dict(self.market_epochs),
            "books": {market: book.as_dict() for market, book in self.books.items()},
            "commands": {"cached": len(self.command_records), "pending": sum(record.status in {"ACCEPTED", "PENDING", "UNKNOWN_TIMEOUT"} for record in self.command_records.values())},
            "counters": dict(self.counters),
            "alerts": self.alerts.snapshot(now_ms=timestamp),
            "metrics": self.metrics_snapshot,
            "process_tree": process_tree,
        }

    async def _handle_uds(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=2.0)
            request = json.loads(raw.decode("utf-8"))
            if request.get("action") == "status":
                response = self.status()
            elif request.get("action") == "command":
                if str(request.get("api_key") or "") != self.api_key:
                    response = {"status": "UNAUTHORIZED"}
                else:
                    response = await self.submit(
                        str(request.get("market_id") or ""),
                        GatewayCommandRequest.model_validate(request.get("request") or {}),
                    )
            else:
                response = {"status": "INVALID_ACTION"}
            writer.write(json.dumps(response, ensure_ascii=False, default=str).encode("utf-8") + b"\n")
            await writer.drain()
        except Exception as exc:
            writer.write(json.dumps({"status": "ERROR", "reason": str(exc)[:300]}).encode("utf-8") + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


def create_gateway_app(state: GatewayState) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await state.start()
        try:
            yield
        finally:
            await state.stop()

    app = FastAPI(title="做市流动性测试多进程 Gateway", version="1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    async def require_key(x_api_key: str = Header(default="")) -> None:
        if x_api_key != state.api_key:
            raise HTTPException(status_code=401, detail="invalid API key")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return state.status()

    @app.get("/api/v2/orderbooks/{market_id}")
    async def orderbook(market_id: str) -> dict[str, Any]:
        book = state.books.get(market_id.upper())
        if book is None:
            raise HTTPException(status_code=503, detail="worker snapshot not ready")
        return book.as_dict()

    @app.get("/api/v2/commands/{command_id}", dependencies=[Depends(require_key)])
    async def command_status(command_id: str) -> dict[str, Any]:
        record = state.command_records.get(command_id)
        if record is None:
            raise HTTPException(status_code=404, detail="command not found")
        return record.public()

    @app.post("/api/v2/commands/{market_id}", dependencies=[Depends(require_key)])
    async def command(market_id: str, request: GatewayCommandRequest) -> dict[str, Any]:
        return await state.submit(market_id, request)

    @app.post("/api/v2/maker-plans/{market_id}", dependencies=[Depends(require_key)])
    async def maker_plan(market_id: str, request: GatewayCommandRequest) -> dict[str, Any]:
        return await state.submit(market_id, request, maker_plan=True)

    @app.get("/api/v2/metrics", dependencies=[Depends(require_key)])
    async def metrics() -> dict[str, Any]:
        return state.metrics_snapshot

    @app.get("/api/v2/runtime")
    async def runtime() -> dict[str, Any]:
        return state.status()

    @app.post("/api/v2/runtime/actions", dependencies=[Depends(require_key)])
    async def runtime_action(request: RuntimeActionRequest) -> dict[str, Any]:
        if state.supervisor_control_queue is None:
            raise HTTPException(status_code=503, detail="supervisor control queue unavailable")
        action_id = str(uuid4())
        payload = request.model_dump()
        payload.update({"action_id": action_id, "at": now_ms()})
        try:
            state.supervisor_control_queue.put_nowait(payload)
        except Full as exc:
            raise HTTPException(status_code=503, detail="supervisor control queue full") from exc
        state.counters["runtime_actions"] += 1
        state.action_records[action_id] = {"status": "ACCEPTED", **payload}
        state.action_records.move_to_end(action_id)
        while len(state.action_records) > state.action_record_limit:
            state.action_records.popitem(last=False)
        return {"status": "ACCEPTED", "action_id": action_id, "action": request.action, "target": request.target, "impact": request.impact}

    @app.get("/api/v2/runtime/actions/{action_id}", dependencies=[Depends(require_key)])
    async def runtime_action_status(action_id: str) -> dict[str, Any]:
        record = state.action_records.get(action_id)
        result_path = Path(str(state.config.get("action_results_dir") or "")) / f"{action_id}.json"
        if result_path.exists():
            try:
                return json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        if record is None:
            raise HTTPException(status_code=404, detail="action not found")
        return record

    @app.get("/api/v2/runtime/run-summary", dependencies=[Depends(require_key)])
    async def runtime_run_summary() -> dict[str, Any]:
        if state.supervisor_control_queue is None:
            raise HTTPException(status_code=503, detail="supervisor control queue unavailable")
        action_id = str(uuid4())
        state.supervisor_control_queue.put_nowait({"action": "export_run_summary", "action_id": action_id, "target": "", "impact": "operator export"})
        return {"status": "ACCEPTED", "action_id": action_id, "impact": "writes one JSON summary under this run directory"}

    @app.websocket("/ws/v2/public")
    async def public_ws(websocket: WebSocket, market_id: str | None = Query(default=None)) -> None:
        market = market_id.upper() if market_id else None
        if market is not None and market not in MARKETS:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        client = SocketClient(websocket=websocket, market_id=market, user_id=None)
        state.public_clients.add(client)
        try:
            snapshots = [state.books[item] for item in MARKETS if item in state.books and (market is None or item == market)]
            for snapshot in snapshots:
                await websocket.send_json(snapshot.as_dict())
            while True:
                payload = await client.queue.get()
                try:
                    await asyncio.wait_for(websocket.send_json(payload), timeout=0.5)
                except asyncio.TimeoutError:
                    state.counters["ws_send_timeouts"] += 1
                    state._record_alert(
                        "public websocket send timeout",
                        severity="DEGRADED",
                        priority=1,
                        code="ws_send_timeout",
                    )
                    raise
                finally:
                    client.queue.task_done()
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            state.public_clients.discard(client)

    @app.websocket("/ws/v2/private")
    async def private_ws(
        websocket: WebSocket,
        user_id: int = Query(..., ge=1),
        api_key: str = Query(default=""),
    ) -> None:
        if api_key != state.api_key:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        client = SocketClient(websocket=websocket, market_id=None, user_id=user_id)
        state.private_clients.setdefault(user_id, set()).add(client)
        try:
            while True:
                payload = await client.queue.get()
                try:
                    await asyncio.wait_for(websocket.send_json(payload), timeout=0.5)
                except asyncio.TimeoutError:
                    state.counters["ws_send_timeouts"] += 1
                    state._record_alert(
                        "private websocket send timeout",
                        severity="DEGRADED",
                        priority=1,
                        code="ws_send_timeout",
                    )
                    raise
                finally:
                    client.queue.task_done()
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            clients = state.private_clients.get(user_id)
            if clients is not None:
                clients.discard(client)
                if not clients:
                    state.private_clients.pop(user_id, None)

    return app


async def _gateway_serve(
    config: dict[str, Any],
    queues: dict[str, Any],
    metrics_ingress_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    state = GatewayState(config, queues)
    app = create_gateway_app(state)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=str(config.get("host") or "127.0.0.1"),
            port=int(config["port"]),
            log_level=str(config.get("log_level") or "warning"),
            access_log=False,
        )
    )

    async def stop_watcher() -> None:
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        server.should_exit = True

    async def ready_watcher() -> None:
        while not server.started and not stop_event.is_set():
            await asyncio.sleep(0.02)
        if server.started:
            ready_queue.put(
                {
                    "component": str(config["component"]),
                    "pid": os.getpid(),
                    "status": "READY",
                    "port": int(config["port"]),
                    "uds_path": str(config["uds_path"]),
                    "at": now_ms(),
                }
            )
            try:
                metrics_ingress_queue.put_nowait(
                    {"type": "REGISTER", "component": str(config["component"]), "pid": os.getpid()}
                )
            except Full:
                pass

    await asyncio.gather(server.serve(), stop_watcher(), ready_watcher())


def gateway_process_main(
    config: dict[str, Any],
    queues: dict[str, Any],
    metrics_ingress_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    asyncio.run(_gateway_serve(config, queues, metrics_ingress_queue, ready_queue, stop_event))
