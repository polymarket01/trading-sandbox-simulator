from __future__ import annotations

import asyncio
from decimal import Decimal
import json
import math
import os
from queue import Full
import time
from typing import Any
from uuid import uuid4

from app.multiprocess.protocol import IPCEnvelope, now_ms, put_latest


class LatestReferenceFeed:
    """Feed-side latest state; it never owns an order book or history."""

    def __init__(self, *, run_id: str, stream_epoch: str, worker_queues: dict[str, Any], sampler_queues: dict[str, Any]) -> None:
        self.run_id = run_id
        self.stream_epoch = stream_epoch
        self.worker_queues = worker_queues
        self.sampler_queues = sampler_queues
        self.sequence = 0
        self.latest: dict[str, IPCEnvelope] = {}
        self.metrics = {"updates": 0, "worker_drops": 0, "sampler_drops": 0, "reconnects": 0}

    def publish(self, market_id: str, *, bid: Any, ask: Any, source_timestamp: int, source: str) -> None:
        market = str(market_id).upper()
        bid_value = Decimal(str(bid))
        ask_value = Decimal(str(ask))
        if bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
            return
        self.sequence += 1
        envelope = IPCEnvelope.build(
            run_id=self.run_id,
            market_id=market,
            stream_epoch=self.stream_epoch,
            kind="REFERENCE_UPDATE",
            payload={
                "bid": str(bid_value),
                "ask": str(ask_value),
                "mid": str((bid_value + ask_value) / Decimal("2")),
                "source_timestamp": int(source_timestamp),
                "source": str(source),
            },
            command_id=f"feed-{self.stream_epoch}-{market}-{self.sequence}",
            command_sequence=self.sequence,
            priority_sequence=self.sequence,
            sent_at=now_ms(),
            deadline=now_ms() + 5_000,
        )
        previous = self.latest.get(market)
        if previous is not None and int(previous.payload.get("source_timestamp") or 0) > source_timestamp:
            return
        self.latest[market] = envelope
        self.metrics["updates"] += 1
        self.metrics["worker_drops"] += put_latest(self.worker_queues[market], envelope)
        self.metrics["sampler_drops"] += put_latest(self.sampler_queues[market], envelope)


async def _synthetic_loop(feed: LatestReferenceFeed, stop_event: Any, interval_ms: int) -> None:
    started = time.monotonic()
    while not stop_event.is_set():
        phase = time.monotonic() - started
        mid = Decimal("62000") + Decimal(str(round(math.sin(phase / 4) * 25, 4)))
        spread = Decimal("0.10")
        timestamp = now_ms()
        feed.publish("BTCUSDT", bid=mid - spread, ask=mid + spread, source_timestamp=timestamp, source="synthetic")
        feed.publish("BTCUSDT-PERP", bid=mid - spread, ask=mid + spread, source_timestamp=timestamp, source="synthetic")
        await asyncio.sleep(max(0.005, interval_ms / 1000))


async def _binance_market_loop(
    feed: LatestReferenceFeed,
    stop_event: Any,
    *,
    market_id: str,
    url: str,
) -> None:
    import websockets

    backoff = 1.0
    while not stop_event.is_set():
        try:
            async with websockets.connect(
                url,
                open_timeout=10,
                close_timeout=2,
                ping_interval=20,
                ping_timeout=10,
                max_queue=1,
            ) as websocket:
                backoff = 1.0
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(websocket.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    payload = json.loads(raw)
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                    feed.publish(
                        market_id,
                        bid=data.get("b"),
                        ask=data.get("a"),
                        source_timestamp=int(data.get("E") or now_ms()),
                        source="binance_book_ticker",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            feed.metrics["reconnects"] += 1
            deadline = time.monotonic() + backoff
            while not stop_event.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
            backoff = min(30.0, backoff * 2)


async def _feed_async(
    config: dict[str, Any],
    worker_queues: dict[str, Any],
    sampler_queues: dict[str, Any],
    metrics_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    epoch = str(uuid4())
    feed = LatestReferenceFeed(
        run_id=str(config["run_id"]),
        stream_epoch=epoch,
        worker_queues=worker_queues,
        sampler_queues=sampler_queues,
    )
    ready_queue.put(
        {
            "component": str(config["component"]),
            "pid": os.getpid(),
            "stream_epoch": epoch,
            "status": "READY",
            "mode": str(config.get("mode", "binance")),
            "at": now_ms(),
        }
    )
    mode = str(config.get("mode", "binance")).lower()
    if mode == "synthetic":
        tasks = [asyncio.create_task(_synthetic_loop(feed, stop_event, int(config.get("interval_ms", 20))))]
    else:
        tasks = [
            asyncio.create_task(
                _binance_market_loop(
                    feed,
                    stop_event,
                    market_id="BTCUSDT",
                    url="wss://stream.binance.com:9443/ws/btcusdt@bookTicker",
                )
            ),
            asyncio.create_task(
                _binance_market_loop(
                    feed,
                    stop_event,
                    market_id="BTCUSDT-PERP",
                    url="wss://fstream.binance.com/ws/btcusdt@bookTicker",
                )
            ),
        ]
    try:
        while not stop_event.is_set():
            snapshot = {
                "component": str(config["component"]),
                "pid": os.getpid(),
                "at": now_ms(),
                "stream_epoch": epoch,
                "mode": mode,
                "latest": {
                    market: {
                        "source_timestamp": envelope.payload.get("source_timestamp"),
                        "received_at": envelope.sent_at,
                    }
                    for market, envelope in feed.latest.items()
                },
                **feed.metrics,
            }
            try:
                metrics_queue.put_nowait(snapshot)
            except Full:
                pass
            await asyncio.sleep(0.5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def feed_process_main(
    config: dict[str, Any],
    worker_queues: dict[str, Any],
    sampler_queues: dict[str, Any],
    metrics_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    asyncio.run(_feed_async(config, worker_queues, sampler_queues, metrics_queue, ready_queue, stop_event))
