from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections import defaultdict, deque

from fastapi import WebSocket

from exchange_common.quote_pipeline import LatencyTracker


SEND_TIMEOUT_SECONDS = 0.25
ORDERBOOK_WS_DEBOUNCE_MS = max(5, int(os.getenv("ORDERBOOK_WS_DEBOUNCE_MS", "20")))


class WebSocketManager:
    def __init__(self, *, stream_id: str = "unknown") -> None:
        self.stream_id = str(stream_id)
        self.public_subscriptions: dict[str, set[WebSocket]] = defaultdict(set)
        self.public_orderbook_depths: dict[WebSocket, dict[str, int]] = defaultdict(dict)
        self.private_subscriptions: dict[tuple[int, str], set[WebSocket]] = defaultdict(set)
        self.authenticated_users: dict[WebSocket, int] = {}
        self._send_locks: dict[WebSocket, asyncio.Lock] = {}
        self._orderbook_last_sent: dict[WebSocket, dict[str, tuple[str, int]]] = defaultdict(dict)
        self._orderbook_last_frame: dict[WebSocket, dict[str, str]] = defaultdict(dict)
        self._orderbook_last_book_fingerprint: dict[WebSocket, dict[str, str]] = defaultdict(dict)
        self._orderbook_initialized: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._orderbook_pending: dict[str, dict] = {}
        self._orderbook_latest_seq: dict[str, int] = {}
        self._orderbook_last_flush_ms: dict[str, float] = {}
        self._orderbook_flush_tasks: dict[str, asyncio.Task] = {}
        self._orderbook_flush_locks: dict[str, asyncio.Lock] = {}
        self._orderbook_nowait_pending: dict[str, dict] = {}
        self._orderbook_nowait_tasks: dict[str, asyncio.Task] = {}
        self._orderbook_publish_latency_ms: deque[float] = deque(maxlen=4096)
        self._stage_latency = LatencyTracker()
        self._started_at = time.monotonic()
        self.metrics: dict[str, int] = {
            "broadcast_calls": 0,
            "messages_attempted": 0,
            "send_timeouts": 0,
            "send_failures": 0,
            "dropped_sockets": 0,
            "orderbook_stale_payloads_dropped": 0,
            "orderbook_same_seq_conflicts": 0,
            "orderbook_pre_snapshot_deltas_dropped": 0,
            "orderbook_gap_snapshots": 0,
            "orderbook_socket_resyncs": 0,
            "orderbook_delta_messages": 0,
            "orderbook_snapshot_messages": 0,
            "orderbook_json_bytes_sent": 0,
            "orderbook_publish_p50_ms": 0.0,
            "orderbook_publish_p95_ms": 0.0,
            "orderbook_publish_p99_ms": 0.0,
            "orderbook_published_to_ws_p50_ms": 0.0,
            "orderbook_published_to_ws_p95_ms": 0.0,
            "orderbook_published_to_ws_p99_ms": 0.0,
            "orderbook_nowait_coalesced": 0,
        }

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._send_locks.setdefault(websocket, asyncio.Lock())

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._discard_locked(websocket)

    async def auth_private(self, websocket: WebSocket, user_id: int) -> None:
        async with self._lock:
            self.authenticated_users[websocket] = user_id

    async def subscribe_public(
        self,
        websocket: WebSocket,
        channel: str,
        symbol: str,
        interval: str | None = None,
        *,
        depth: int | None = None,
    ) -> None:
        key = f"{channel}:{symbol}:{interval or ''}"
        async with self._lock:
            self.public_subscriptions[key].add(websocket)
            if channel == "orderbook" and depth is not None:
                self.public_orderbook_depths[websocket][symbol] = max(1, int(depth))

    async def subscribed_public_symbols(self, channel: str) -> set[str]:
        prefix = f"{channel}:"
        async with self._lock:
            symbols: set[str] = set()
            for key, sockets in self.public_subscriptions.items():
                if not sockets or not key.startswith(prefix):
                    continue
                parts = key.split(":", 2)
                if len(parts) >= 2 and parts[1]:
                    symbols.add(parts[1])
            return symbols

    async def subscribe_private(self, websocket: WebSocket, channel: str) -> None:
        async with self._lock:
            user_id = self.authenticated_users[websocket]
            self.private_subscriptions[(user_id, channel)].add(websocket)

    async def broadcast_public(self, channel: str, symbol: str, payload: dict, interval: str | None = None) -> None:
        key = f"{channel}:{symbol}:{interval or ''}"
        await self._broadcast(self.public_subscriptions[key], payload)

    async def broadcast_public_orderbook(
        self,
        symbol: str,
        payload: dict,
        *,
        default_depth: int = 50,
        immediate: bool = False,
    ) -> None:
        payload = dict(payload)
        payload.setdefault("_published_at_monotonic", time.perf_counter())
        payload.setdefault("stream_id", self.stream_id)
        try:
            sequence = int(payload["seq"])
        except (KeyError, TypeError, ValueError):
            sequence = None
        if sequence is not None:
            latest_sequence = self._orderbook_latest_seq.get(symbol)
            if latest_sequence is not None and sequence < latest_sequence:
                # Event broadcasts and the periodic snapshot loop can race.  A
                # late periodic payload must never roll a subscriber backwards.
                self.metrics["orderbook_stale_payloads_dropped"] += 1
                return
            self._orderbook_latest_seq[symbol] = sequence
        if immediate or ORDERBOOK_WS_DEBOUNCE_MS <= 0:
            await self._flush_public_orderbook_now(symbol, payload, default_depth=default_depth)
            return
        self._orderbook_pending[symbol] = payload
        now_ms = time.monotonic() * 1000
        last_flush_ms = self._orderbook_last_flush_ms.get(symbol, 0.0)
        elapsed_ms = now_ms - last_flush_ms
        if elapsed_ms >= ORDERBOOK_WS_DEBOUNCE_MS:
            await self._flush_public_orderbook_now(symbol, self._orderbook_pending.pop(symbol, payload), default_depth=default_depth)
            return
        existing = self._orderbook_flush_tasks.get(symbol)
        if existing is not None and not existing.done():
            return
        delay_seconds = max(0.0, (ORDERBOOK_WS_DEBOUNCE_MS - elapsed_ms) / 1000)
        self._orderbook_flush_tasks[symbol] = asyncio.create_task(
            self._flush_public_orderbook_delayed(symbol, default_depth=default_depth, delay_seconds=delay_seconds),
        )

    def broadcast_public_orderbook_nowait(
        self,
        symbol: str,
        payload: dict,
        *,
        default_depth: int = 50,
        immediate: bool = False,
    ) -> asyncio.Task:
        """Queue one latest-wins broadcast task per symbol.

        The old implementation created one Task per fast-path update.  A
        slow socket could retain thousands of full payloads and their task
        frames.  Keep only the newest payload while one sender is active.
        """
        key = str(symbol).upper()
        current = self._orderbook_nowait_pending.get(key)
        if current is not None:
            self._orderbook_nowait_pending[key] = dict(payload)
            self.metrics["orderbook_nowait_coalesced"] += 1
        else:
            self._orderbook_nowait_pending[key] = dict(payload)
        task = self._orderbook_nowait_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._drain_orderbook_nowait(key, default_depth=default_depth, immediate=immediate),
                name=f"ws-orderbook-nowait-{key}",
            )
            self._orderbook_nowait_tasks[key] = task
        return task

    async def _drain_orderbook_nowait(self, symbol: str, *, default_depth: int, immediate: bool) -> None:
        try:
            while True:
                payload = self._orderbook_nowait_pending.pop(symbol, None)
                if payload is None:
                    return
                await self.broadcast_public_orderbook(
                    symbol,
                    payload,
                    default_depth=default_depth,
                    immediate=immediate,
                )
        finally:
            self._orderbook_nowait_tasks.pop(symbol, None)

    async def _flush_public_orderbook_now(self, symbol: str, payload: dict, *, default_depth: int) -> None:
        # A debounced payload can already be queued when a newer immediate
        # event arrives.  Serialize each symbol through the actual send point
        # and re-check there, otherwise subscribers can still observe
        # e.g. 10 -> 12 -> 11 even though the ingress check rejected newly
        # arriving stale payloads.
        flush_lock = self._orderbook_flush_locks.setdefault(symbol, asyncio.Lock())
        async with flush_lock:
            try:
                sequence = int(payload["seq"])
            except (KeyError, TypeError, ValueError):
                sequence = None
            latest_sequence = self._orderbook_latest_seq.get(symbol)
            if sequence is not None and latest_sequence is not None and sequence < latest_sequence:
                self.metrics["orderbook_stale_payloads_dropped"] += 1
                return
            self._orderbook_last_flush_ms[symbol] = time.monotonic() * 1000
            started = time.perf_counter()
            published_at = payload.get("_published_at_monotonic")
            if isinstance(published_at, (int, float)):
                self._stage_latency.observe("published_book_to_ws", (started - float(published_at)) * 1000)
            await self._broadcast_public_orderbook_now(symbol, payload, default_depth=default_depth)
            self._orderbook_publish_latency_ms.append((time.perf_counter() - started) * 1000)
            # Recording a frame must stay O(1). Sorting the full 4096-sample
            # window three times per frame made telemetry dominate live CPU.
            # Compute all percentiles together when metrics are requested.

    async def _flush_public_orderbook_delayed(self, symbol: str, *, default_depth: int, delay_seconds: float) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            payload = self._orderbook_pending.pop(symbol, None)
            if payload is not None:
                await self._flush_public_orderbook_now(symbol, payload, default_depth=default_depth)
        finally:
            self._orderbook_flush_tasks.pop(symbol, None)

    async def _broadcast_public_orderbook_now(self, symbol: str, payload: dict, *, default_depth: int = 50) -> None:
        key = f"orderbook:{symbol}:"
        targets = list(self.public_subscriptions[key])
        if not targets:
            return
        self.metrics["broadcast_calls"] += 1
        self.metrics["messages_attempted"] += len(targets)
        is_delta = str(payload.get("type") or "snapshot").lower() == "delta"
        if is_delta:
            self.metrics["orderbook_delta_messages"] += 1
        else:
            self.metrics["orderbook_snapshot_messages"] += 1
        results = await asyncio.gather(
            *(
                self._send_orderbook_with_timeout(
                    websocket,
                    symbol,
                    self._payload_for_socket(
                        payload,
                        self.public_orderbook_depths.get(websocket, {}).get(symbol, default_depth),
                        websocket=websocket,
                        default_depth=default_depth,
                    ),
                )
                for websocket in targets
            ),
            return_exceptions=True,
        )
        stale = [
            websocket
            for websocket, result in zip(targets, results)
            if result not in {"ok", "stale"} or isinstance(result, Exception)
        ]
        if stale:
            self.metrics["dropped_sockets"] += len(stale)
            for websocket in stale:
                await self._drop_socket(websocket)

    async def send_public_orderbook_snapshot(
        self,
        websocket: WebSocket,
        symbol: str,
        payload: dict,
        *,
        depth: int,
    ) -> None:
        """Send a subscription snapshot through the normal ordered send path."""
        snapshot = dict(payload)
        snapshot.setdefault("stream_id", self.stream_id)
        result = await self._send_orderbook_with_timeout(
            websocket,
            symbol,
            self._payload_for_socket(snapshot, depth, websocket=websocket, default_depth=depth),
        )
        if result not in {"ok", "stale"}:
            self.metrics["dropped_sockets"] += 1
            await self._drop_socket(websocket)

    async def broadcast_private(self, user_id: int, channel: str, payload: dict) -> None:
        await self._broadcast(self.private_subscriptions[(user_id, channel)], payload)

    async def _broadcast(self, sockets: set[WebSocket], payload: dict) -> None:
        targets = list(sockets)
        if not targets:
            return
        self.metrics["broadcast_calls"] += 1
        self.metrics["messages_attempted"] += len(targets)
        results = await asyncio.gather(
            *(self._send_with_timeout(websocket, payload) for websocket in targets),
            return_exceptions=True,
        )
        stale = [
            websocket
            for websocket, result in zip(targets, results)
            if result != "ok" or isinstance(result, Exception)
        ]
        if stale:
            self.metrics["dropped_sockets"] += len(stale)
            for websocket in stale:
                await self._drop_socket(websocket)

    async def _drop_socket(self, websocket: WebSocket) -> None:
        """Remove a stale socket and actively close it.

        Closing the TCP connection lets the client see onclose and reconnect.
        Without this, a send-timeout drop leaves a zombie connection that the
        client keeps open while receiving nothing (frozen orderbook/TAS).
        """
        async with self._lock:
            self._discard_locked(websocket)
        with contextlib.suppress(Exception):
            await websocket.close(code=1012)

    async def _send_with_timeout(self, websocket: WebSocket, payload: dict) -> str:
        async def send_locked() -> None:
            lock = self._send_locks.setdefault(websocket, asyncio.Lock())
            async with lock:
                await websocket.send_json(payload)

        try:
            await asyncio.wait_for(send_locked(), timeout=SEND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.metrics["send_timeouts"] += 1
            return "timeout"
        except Exception:
            self.metrics["send_failures"] += 1
            return "error"
        return "ok"

    async def _send_orderbook_with_timeout(self, websocket: WebSocket, symbol: str, payload: dict) -> str:
        async def send_locked() -> str:
            lock = self._send_locks.setdefault(websocket, asyncio.Lock())
            async with lock:
                send_payload = payload
                stream_id = str(send_payload.get("stream_id") or self.stream_id)
                try:
                    sequence = int(send_payload["seq"])
                except (KeyError, TypeError, ValueError):
                    sequence = None
                last_sent = self._orderbook_last_sent[websocket].get(symbol)
                is_snapshot = str(send_payload.get("type") or "snapshot").lower() == "snapshot"
                if not is_snapshot:
                    # A delta has no independent baseline. A subscription can
                    # race a live broadcast, so suppress deltas until its
                    # initial snapshot has actually crossed the socket.
                    if websocket not in self._orderbook_initialized:
                        self.metrics["orderbook_pre_snapshot_deltas_dropped"] += 1
                        return "stale"
                    # Debounce/latest-wins publishing may intentionally
                    # coalesce frames. Never let a client apply the newest
                    # delta to an older baseline. Prefer an in-band snapshot
                    # from the authoritative fingerprint; only close when the
                    # full book is unavailable.
                    if (
                        sequence is None
                        or last_sent is None
                        or last_sent[0] != stream_id
                        or sequence > last_sent[1] + 1
                    ):
                        snapshot = self._snapshot_from_book_fingerprint(send_payload)
                        if snapshot is None:
                            self.metrics["orderbook_socket_resyncs"] += 1
                            return "resync"
                        send_payload = snapshot
                        is_snapshot = True
                        self.metrics["orderbook_gap_snapshots"] += 1
                if (
                    sequence is not None
                    and last_sent is not None
                    and last_sent[0] == stream_id
                    and sequence < last_sent[1]
                ):
                    self.metrics["orderbook_stale_payloads_dropped"] += 1
                    return "stale"
                frame = self._frame_fingerprint(send_payload)
                previous_frame = self._orderbook_last_frame.get(websocket, {}).get(symbol)
                book_fingerprint = str(send_payload.get("_book_fingerprint") or "")
                previous_book_fingerprint = self._orderbook_last_book_fingerprint.get(websocket, {}).get(symbol, "")
                is_heartbeat = is_snapshot and bool(send_payload.get("heartbeat"))
                if sequence is not None and last_sent is not None and last_sent[0] == stream_id and sequence == last_sent[1]:
                    if (
                        not is_heartbeat
                        and (
                            (
                                book_fingerprint
                                and previous_book_fingerprint
                                and book_fingerprint == previous_book_fingerprint
                            )
                            or previous_frame == frame
                        )
                    ):
                        return "stale"
                    if not is_heartbeat:
                        self.metrics["orderbook_same_seq_conflicts"] += 1
                        return "stale"
                wire = send_payload.pop("__wire", None)
                if wire and hasattr(websocket, "send_text"):
                    await websocket.send_text(wire)
                    self.metrics["orderbook_json_bytes_sent"] += len(wire.encode("utf-8"))
                else:
                    await websocket.send_json(self._clean_payload(send_payload))
                    self.metrics["orderbook_json_bytes_sent"] += len(
                        json.dumps(self._clean_payload(send_payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    )
                if sequence is not None:
                    self._orderbook_last_sent[websocket][symbol] = (stream_id, sequence)
                    self._orderbook_last_frame[websocket][symbol] = frame
                    if book_fingerprint:
                        self._orderbook_last_book_fingerprint[websocket][symbol] = book_fingerprint
                    if is_snapshot:
                        self._orderbook_initialized.add(websocket)
                return "ok"

        try:
            return await asyncio.wait_for(send_locked(), timeout=SEND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.metrics["send_timeouts"] += 1
            return "timeout"
        except Exception:
            self.metrics["send_failures"] += 1
            return "error"

    def metrics_snapshot(self) -> dict[str, int]:
        self._refresh_publish_latency_metrics()
        published_to_ws = self._stage_latency.snapshot().get("published_book_to_ws", {})
        for percentile in ("p50", "p95", "p99"):
            self.metrics[f"orderbook_published_to_ws_{percentile}_ms"] = float(
                published_to_ws.get(f"{percentile}_ms", 0.0) or 0.0
            )
        self.metrics["orderbook_json_bytes_per_second"] = int(
            self.metrics["orderbook_json_bytes_sent"] / max(0.001, time.monotonic() - self._started_at)
        )
        return dict(self.metrics)

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
        return round(float(ordered[index]), 3)

    def _refresh_publish_latency_metrics(self) -> None:
        ordered = sorted(self._orderbook_publish_latency_ms)
        for percentile in (50, 95, 99):
            index = min(len(ordered) - 1, max(0, int(round((percentile / 100) * (len(ordered) - 1)))))
            self.metrics[f"orderbook_publish_p{percentile}_ms"] = round(float(ordered[index]), 3) if ordered else 0.0

    @staticmethod
    def _clean_payload(payload: dict) -> dict:
        return {key: value for key, value in payload.items() if not str(key).startswith("_") and key != "__wire"}

    @staticmethod
    def _snapshot_from_book_fingerprint(payload: dict) -> dict | None:
        fingerprint = payload.get("_book_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            return None
        try:
            book = json.loads(fingerprint)
            bids = book["bids"]
            asks = book["asks"]
            depth = max(1, int(payload.get("depth") or 50))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(bids, (list, tuple)) or not isinstance(asks, (list, tuple)):
            return None
        return {
            "channel": payload.get("channel", "orderbook"),
            "type": "snapshot",
            "symbol": payload.get("symbol"),
            "stream_id": payload.get("stream_id"),
            "seq": payload.get("seq"),
            "ts": payload.get("ts"),
            "depth": depth,
            "bids": [list(item) for item in bids[:depth]],
            "asks": [list(item) for item in asks[:depth]],
            "source": "gap_resync",
            "_book_fingerprint": fingerprint,
        }

    @staticmethod
    def _frame_fingerprint(payload: dict) -> str:
        cached = payload.get("_frame_fingerprint")
        if cached is not None:
            return str(cached)
        content = WebSocketManager._clean_payload(payload)
        # Publisher/source is diagnostic metadata. It must not make two
        # identical book frames at the same sequence look different.
        content.pop("source", None)
        return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _payload_for_socket(self, payload: dict, depth: int, *, websocket: WebSocket, default_depth: int) -> dict:
        requested_depth = max(1, int(depth or default_depth or 50))
        cache = payload.get("_wire_cache")
        wire = None
        if isinstance(cache, (tuple, list)):
            for cached_depth, cached_wire in cache:
                if int(cached_depth) == requested_depth:
                    wire = str(cached_wire)
                    break
        if wire is not None and hasattr(websocket, "send_text"):
            # Real Starlette sockets can send the pre-serialized depth frame
            # directly. Do not materialize/copy the full canonical depth just
            # to slice it per socket; the wire itself is also the stable frame
            # identity used for same-seq de-duplication.
            if payload.get("stream_epoch"):
                # Older cached wire entries may have been built before the
                # epoch field was added.  Add it only at the connection edge;
                # the immutable book cache remains unchanged.
                try:
                    wire_body = json.loads(wire)
                    if wire_body.get("stream_epoch") != payload.get("stream_epoch"):
                        wire_body["stream_epoch"] = payload.get("stream_epoch")
                        wire = json.dumps(wire_body, ensure_ascii=False, separators=(",", ":"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return {
                "channel": payload.get("channel", "orderbook"),
                "type": payload.get("type", "snapshot"),
                "symbol": payload.get("symbol"),
                "stream_id": payload.get("stream_id", self.stream_id),
                "stream_epoch": payload.get("stream_epoch"),
                "seq": payload.get("seq"),
                "ts": payload.get("ts"),
                "depth": requested_depth,
                "__wire": wire,
                "_frame_fingerprint": wire,
                "_book_fingerprint": payload.get("_book_fingerprint"),
            }
        sliced = self._slice_orderbook_payload(payload, requested_depth)
        return sliced

    @staticmethod
    def _slice_orderbook_payload(payload: dict, depth: int) -> dict:
        sliced = dict(payload)
        sliced["bids"] = list(payload.get("bids") or [])[:depth]
        sliced["asks"] = list(payload.get("asks") or [])[:depth]
        sliced["depth"] = depth
        return sliced

    def _discard_locked(self, websocket: WebSocket) -> None:
        for subscribers in self.public_subscriptions.values():
            subscribers.discard(websocket)
        for subscribers in self.private_subscriptions.values():
            subscribers.discard(websocket)
        self.public_orderbook_depths.pop(websocket, None)
        self._orderbook_last_sent.pop(websocket, None)
        self._orderbook_initialized.discard(websocket)
        if hasattr(self, "_orderbook_last_frame"):
            self._orderbook_last_frame.pop(websocket, None)
        if hasattr(self, "_orderbook_last_book_fingerprint"):
            self._orderbook_last_book_fingerprint.pop(websocket, None)
        self.authenticated_users.pop(websocket, None)
        self._send_locks.pop(websocket, None)
