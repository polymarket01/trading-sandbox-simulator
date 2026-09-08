from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import os
from pathlib import Path
from queue import Empty, Full
import time
from typing import Any

from app.multiprocess.protocol import IPCEnvelope, drain_latest, now_ms
from app.services.history_store import HistoryStore
from app.services.market_data_service import MarketDataService


MARKET_NUMERIC_IDS = {"BTCUSDT": 1, "BTCUSDT-PERP": 2}


async def _sampler_async(
    config: dict[str, Any],
    sample_queues: dict[str, Any],
    metrics_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    run_id = str(config["run_id"])
    history_path = Path(str(config["history_path"]))
    store = HistoryStore(
        history_path,
        run_id=run_id,
        data_root=history_path.parent,
        queue_max=max(8, int(config.get("writer_queue_max", 4_096))),
        batch_size=max(1, int(config.get("batch_size", 128))),
        commit_interval_seconds=float(config.get("commit_interval_seconds", 0.25)),
    )
    market_data = MarketDataService()
    latest: dict[str, IPCEnvelope] = {}
    sampled_source_timestamps: dict[str, int] = {}
    last_kline_finalized_at: dict[str, int] = {}
    last_sample_at: dict[str, int] = {}
    counters = {"samples": 0, "carried_forward": 0, "input_drops": 0, "writer_drops": 0, "retention_runs": 0}
    started_at = now_ms()
    await store.start()
    store.enqueue_run_summary(
        {
            "run_id": run_id,
            "started_at_ms": started_at,
            "persistence_mode": "sampled",
            "status": "running",
            "summary": {"owner": "history_sampler_process"},
        }
    )
    ready_queue.put(
        {
            "component": str(config["component"]),
            "pid": os.getpid(),
            "status": "READY",
            "history_path": str(history_path),
            "at": now_ms(),
        }
    )
    next_sample = time.monotonic()
    next_metrics = time.monotonic()
    next_retention = time.monotonic() + max(10.0, float(config.get("retention_interval_seconds", 60)))
    last_summary_minute = -1
    try:
        while not stop_event.is_set():
            for market_id, queue in sample_queues.items():
                item, dropped = drain_latest(queue)
                counters["input_drops"] += dropped
                if isinstance(item, IPCEnvelope):
                    try:
                        item.validate(expected_run_id=run_id)
                    except ValueError:
                        continue
                    if item.market_id == market_id:
                        latest[market_id] = item

            current = time.monotonic()
            if current >= next_sample:
                next_sample = current + 1.0
                timestamp_ms = (now_ms() // 1_000) * 1_000
                sample_time = datetime.fromtimestamp(timestamp_ms / 1_000, tz=UTC)
                for market_id in sample_queues:
                    envelope = latest.get(market_id)
                    if envelope is None:
                        continue
                    source_timestamp = int(envelope.payload.get("source_timestamp") or envelope.sent_at)
                    carried = sampled_source_timestamps.get(market_id) == source_timestamp
                    sampled_source_timestamps[market_id] = source_timestamp
                    if carried:
                        counters["carried_forward"] += 1
                    mid = Decimal(str(envelope.payload.get("mid")))
                    sample_payload = {
                        "run_id": run_id,
                        "market_id": MARKET_NUMERIC_IDS.get(market_id),
                        "symbol": market_id,
                        "sampled_at_ms": timestamp_ms,
                        "bid": envelope.payload.get("bid"),
                        "ask": envelope.payload.get("ask"),
                        "mid": str(mid),
                        "source": "carried_forward" if carried else str(envelope.payload.get("source") or "binance"),
                        "source_timestamp_ms": source_timestamp,
                        "carried_forward": carried,
                    }
                    last_sample_at[market_id] = timestamp_ms
                    if not store.enqueue_price_sample(sample_payload):
                        counters["writer_drops"] += 1
                    kline_records = market_data.sample_price(
                        market_id,
                        mid,
                        sample_time,
                        source=sample_payload["source"],
                        market_id=MARKET_NUMERIC_IDS.get(market_id),
                    )
                    for payload in kline_records:
                        payload["run_id"] = run_id
                        if bool(payload.get("is_closed")):
                            last_kline_finalized_at[market_id] = max(
                                int(last_kline_finalized_at.get(market_id) or 0),
                                int(payload.get("close_time_ms") or payload.get("updated_at_ms") or timestamp_ms),
                            )
                        if not store.enqueue_kline(payload):
                            counters["writer_drops"] += 1
                    counters["samples"] += 1

                minute = timestamp_ms - timestamp_ms % 60_000
                if minute != last_summary_minute:
                    last_summary_minute = minute
                    for market_id in sample_queues:
                        store.enqueue_minute_summary(
                            {
                                "run_id": run_id,
                                "symbol": market_id,
                                "minute_open_time_ms": minute,
                                "quote_cycles": 0,
                                "kline_freshness_ms": max(
                                    0,
                                    timestamp_ms - int(sampled_source_timestamps.get(market_id) or timestamp_ms),
                                ),
                                "sampling_drops": counters["input_drops"] + counters["writer_drops"],
                                "created_at_ms": timestamp_ms,
                                "payload": {"sampler_pid": os.getpid()},
                            }
                        )

            if current >= next_retention:
                next_retention = current + max(10.0, float(config.get("retention_interval_seconds", 60)))
                await asyncio.to_thread(store.prune_sync)
                counters["retention_runs"] += 1

            if current >= next_metrics:
                next_metrics = current + 1.0
                snapshot = {
                    "component": str(config["component"]),
                    "pid": os.getpid(),
                    "at": now_ms(),
                    "last_sample_at": dict(last_sample_at),
                    "last_kline_finalized_at": dict(last_kline_finalized_at),
                    "sampling_drops": counters["input_drops"] + counters["writer_drops"] + int(getattr(market_data, "sampling_drop_count", 0)),
                    **counters,
                    "storage": store.metrics_snapshot(),
                }
                try:
                    metrics_queue.put_nowait(snapshot)
                except Full:
                    pass
            await asyncio.sleep(0.01)
    finally:
        store.enqueue_run_summary(
            {
                "run_id": run_id,
                "started_at_ms": started_at,
                "ended_at_ms": now_ms(),
                "persistence_mode": "sampled",
                "status": "stopped",
                "summary": counters,
            }
        )
        try:
            await asyncio.wait_for(
                store.stop(flush=True),
                timeout=max(0.5, float(config.get("flush_seconds", 5.0))),
            )
        except asyncio.TimeoutError:
            await store.stop(flush=False)


def sampler_process_main(
    config: dict[str, Any],
    sample_queues: dict[str, Any],
    metrics_queue: Any,
    ready_queue: Any,
    stop_event: Any,
) -> None:
    asyncio.run(_sampler_async(config, sample_queues, metrics_queue, ready_queue, stop_event))
