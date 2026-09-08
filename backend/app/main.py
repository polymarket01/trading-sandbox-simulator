from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.api import admin, auth, causal, contracts, paper, private, public
from app.core.config import settings
from app.core.constants import PRODUCT_TYPE_PERP, PRODUCT_TYPE_SPOT, ROLE_BOT, ZERO
from app.core.security import hash_session_token, verify_ws_signature
from app.core.time_utils import to_millis
from app.db.session import SessionLocal
from app.models.market import Market
from app.models.contract_account import ContractAccount
from app.models.contract_position import ContractPosition
from app.models.exchange_runtime import ExchangeSnapshotRecord
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.models.paper_exchange import PaperSession
from app.seed.bootstrap import bootstrap, load_sampled_runtime_defaults
from app.services.contract_price_service import ContractPriceService
from app.services.order_service import OrderService
from app.services.persistence_writer import PersistenceWriter
from app.services.causal_command_service import CausalCommandService
from app.services.contract_service import ContractService
from app.services.quote_set_service import QuoteSetService
from app.services.contract_maintenance_service import ContractMaintenanceService
from app.services.contract_liquidity_service import ContractLiquidityService
from app.services.bot_orchestrator import BotOrchestratorService
from app.services.history_retention_service import HistoryRetentionService, history_retention_auto_enabled
from app.services.sandbox_db_guard import database_file_path, run_sandbox_db_size_guard, prune_active_history
from app.services.runtime import AppRuntime
from app.services.sqlite_worker_guard import SQLiteWorkerGuard
from app.services.accounting_service import ensure_shadow_opening_balances
from app.services.financial_outbox_service import (
    FinancialOutboxDispatcher,
    deliver_financial_runtime_event,
    ensure_financial_outbox_checkpoint,
)
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.accounting_evidence_service import run_daily_accounting_evidence
from app.services.retired_paper_liquidity import retire_legacy_configuration
from app.services.history_store import HistoryStore
from app.services.persistence_contract import (
    is_runtime_only_mode,

    platform_durable_contract,
    public_persistence_contract,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIST_DIR = Path(os.getenv("FRONTEND_DIST_DIR", str(ROOT_DIR / "frontend" / "dist")))
ORDERBOOK_SNAPSHOT_PUSH_INTERVAL_MS = max(10, int(os.getenv("ORDERBOOK_SNAPSHOT_PUSH_INTERVAL_MS", str(settings.quote_head_interval_ms))))
ORDERBOOK_SNAPSHOT_PUSH_DEPTH = max(1, min(100, int(os.getenv("ORDERBOOK_SNAPSHOT_PUSH_DEPTH", "100"))))
ORDERBOOK_HEARTBEAT_INTERVAL_MS = max(500, int(os.getenv("ORDERBOOK_HEARTBEAT_INTERVAL_MS", str(settings.quote_snapshot_heartbeat_ms))))
ORDERBOOK_CAPTURE_SECONDS = max(0.0, float(os.getenv("ORDERBOOK_CAPTURE_SECONDS", "0")))
ORDERBOOK_CAPTURE_PATH = ROOT_DIR / ".runtime" / "orderbook_capture.jsonl"
RUNNER_PARENT_WATCH_INTERVAL_SECONDS = 2.0
RUNNER_PARENT_HARD_EXIT_SECONDS = 5.0


def runner_parent_is_alive(
    expected_pid: int,
    *,
    actual_parent_pid: int | None = None,
) -> bool:
    if expected_pid <= 0:
        return False
    parent_pid = os.getppid() if actual_parent_pid is None else int(actual_parent_pid)
    if parent_pid != expected_pid:
        return False
    try:
        os.kill(expected_pid, 0)
    except OSError:
        return False
    return True


async def stop_runner_managed_makers(app: FastAPI) -> list[dict]:
    orchestrator: BotOrchestratorService = app.state.bot_orchestrator
    results: list[dict] = []
    for pid_path in sorted(orchestrator.runtime_dir.glob("mm_service_*.pid")):
        symbol = pid_path.name[len("mm_service_") : -len(".pid")]
        if not symbol:
            continue
        try:
            result = await asyncio.to_thread(orchestrator.stop_process, symbol)
            results.append({"symbol": symbol, "result": result})
        except Exception as exc:  # pragma: no cover - last-resort shutdown path
            results.append({"symbol": symbol, "error": str(exc)})
    return results


def runner_parent_watch_failed(state: object) -> bool:
    return bool(
        isinstance(state, dict)
        and state.get("enabled")
        and state.get("status") == "parent_lost"
    )


def terminate_backend_after_parent_loss(
    *,
    grace_seconds: float = RUNNER_PARENT_HARD_EXIT_SECONDS,
) -> threading.Timer:
    """Request graceful shutdown and guarantee that an orphan cannot survive.

    Uvicorn owns SIGTERM and normally exits through the lifespan cleanup.  The
    daemon timer is deliberately outside asyncio: even if shutdown cancels the
    watchdog task or the event loop is wedged, the process is forced down after
    the grace period.
    """

    timer = threading.Timer(max(0.1, float(grace_seconds)), os._exit, args=(1,))
    timer.daemon = True
    timer.start()
    os.kill(os.getpid(), signal.SIGTERM)
    return timer


async def runner_parent_watch_loop(app: FastAPI) -> None:
    expected_pid = int(os.getenv("SANDBOX_RUNNER_PID", "0") or 0)
    state = app.state.runner_parent_watch
    while True:
        await asyncio.sleep(RUNNER_PARENT_WATCH_INTERVAL_SECONDS)
        if runner_parent_is_alive(expected_pid):
            state["last_check_at"] = datetime.now(tz=UTC).isoformat()
            continue
        state.update(
            {
                "status": "parent_lost",
                "detected_at": datetime.now(tz=UTC).isoformat(),
                "actual_parent_pid": os.getppid(),
            }
        )
        state["maker_shutdown"] = await stop_runner_managed_makers(app)
        logging.getLogger("runner_parent_watch").critical(
            "sandbox runner parent lost expected_pid=%s actual_ppid=%s; stopping backend tree",
            expected_pid,
            os.getppid(),
        )
        state["shutdown_requested_at"] = datetime.now(tz=UTC).isoformat()
        state["hard_exit_after_seconds"] = RUNNER_PARENT_HARD_EXIT_SECONDS
        terminate_backend_after_parent_loss()
        return


def normalize_orderbook_depth(value: object, default: int = 20) -> int:
    try:
        requested = int(value) if value is not None else default
    except (TypeError, ValueError):
        requested = default
    return max(1, min(ORDERBOOK_SNAPSHOT_PUSH_DEPTH, requested))


async def stats_loop(app: FastAPI) -> None:
    while True:
        try:
            runtime: AppRuntime = app.state.runtime
            for symbol in list(runtime.engine.books.keys()):
                snapshot, _, _ = await runtime.orderbook_snapshot(symbol)
                stats = runtime.market_data.compute_stats(symbol, snapshot)
                await runtime.ws.broadcast_public(
                    "stats",
                    symbol,
                    {"channel": "stats", "type": "update", "symbol": symbol, "data": stats},
                )
            await asyncio.sleep(settings.stats_push_interval_ms / 1000)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


def _sampled_price_source(runtime: AppRuntime, symbol: str) -> tuple[Decimal | None, str]:
    """Select BBO mid first, then an external/latest-price snapshot."""

    published = runtime.published_orderbooks.get(symbol.upper())
    now_ms = to_millis(datetime.now(tz=UTC))
    if published is not None and published.updated_at_ms > 0 and now_ms - published.updated_at_ms <= 5_000:
        if published.bids and published.asks:
            bid = Decimal(published.bids[0][0])
            ask = Decimal(published.asks[0][0])
            if bid > ZERO and ask > ZERO:
                return (bid + ask) / Decimal("2"), "published_mid"
    snapshots = [
        runtime.contract_price_snapshots.get(symbol.upper()) or {},
        runtime.liquidity_metrics.get(symbol.upper()) or {},
    ]
    for snapshot in snapshots:
        for key in (
            "binance_latest_price",
            "binance_last_price",
            "external_last_price",
            "external_mid",
            "fair_price",
            "last_price",
            "mark_price",
            "index_price",
        ):
            value = snapshot.get(key)
            if value is None:
                continue
            try:
                price = Decimal(str(value))
            except Exception:
                continue
            if price > ZERO:
                return price, "binance_latest"
    return runtime.market_data.sampled_last_price.get(symbol.upper()), "carried_forward"


async def history_sampler_loop(app: FastAPI) -> None:
    """One observation per second; SQLite is only reached through HistoryStore."""

    if True:
        while True:
            await asyncio.sleep(60)
    runtime: AppRuntime = app.state.runtime
    store: HistoryStore = runtime.history_store
    while True:
        started = time.perf_counter()
        try:
            if store.metrics_snapshot().get("sampling_paused"):
                await asyncio.sleep(max(0.2, float(settings.history_sample_interval_seconds)))
                continue
            for market in list(getattr(app.state, "sampled_markets", [])):
                symbol = str(market.symbol).upper()
                price, source = _sampled_price_source(runtime, symbol)
                records = runtime.market_data.sample_price(
                    symbol,
                    price,
                    datetime.now(tz=UTC),
                    source=source,
                    market_id=int(market.id),
                )
                runtime.sampling_metrics["sample_count"] = int(runtime.sampling_metrics.get("sample_count", 0)) + 1
                runtime.sampling_metrics["last_sample_at_ms"] = to_millis(datetime.now(tz=UTC))
                for record in records:
                    if not store.enqueue_kline(record):
                        runtime.sampling_metrics["sampling_drops"] = int(
                            runtime.sampling_metrics.get("sampling_drops", 0)
                        ) + 1
                    else:
                        runtime.sampling_metrics["last_kline_at_ms"] = int(record.get("updated_at_ms") or 0)
            elapsed = time.perf_counter() - started
            await asyncio.sleep(max(0.05, float(settings.history_sample_interval_seconds) - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.sampling_metrics["sampling_drops"] = int(runtime.sampling_metrics.get("sampling_drops", 0)) + 1
            store.enqueue_error("sampler", str(exc))
            await asyncio.sleep(1)


def _metric_number(metrics: dict, *keys: str, default: float = 0) -> float:
    for key in keys:
        value = metrics.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


async def history_minute_summary_loop(app: FastAPI) -> None:
    if True:
        while True:
            await asyncio.sleep(60)
    runtime: AppRuntime = app.state.runtime
    store: HistoryStore = runtime.history_store
    while True:
        try:
            await asyncio.sleep(60)
            minute_ms = (to_millis(datetime.now(tz=UTC)) // 60_000 - 1) * 60_000
            for market in list(getattr(app.state, "sampled_markets", [])):
                symbol = str(market.symbol).upper()
                metrics = dict(runtime.liquidity_metrics.get(symbol) or {})
                writer_metrics = runtime.persistence_writer.metrics_snapshot()
                summary = {
                    "run_id": runtime.run_id,
                    "symbol": symbol,
                    "minute_open_time_ms": minute_ms,
                    "quote_cycles": int(_metric_number(metrics, "quote_cycles", "quotes", "cycles")),
                    "place_count": int(_metric_number(metrics, "place_count", "places", "order_place_count")),
                    "amend_count": int(_metric_number(metrics, "amend_count", "amends", "order_amend_count")),
                    "cancel_count": int(_metric_number(metrics, "cancel_count", "cancels", "order_cancel_count")),
                    "synthetic_fill_count": int(_metric_number(writer_metrics, "robot_flow_synthetic_fills")),
                    "display_fill_count": int(_metric_number(writer_metrics, "robot_flow_synthetic_fills")),
                    "actual_match_count": int(_metric_number(metrics, "actual_match_count", "match_count")),
                    "stp_intercept_count": int(_metric_number(metrics, "stp_intercept_count", "stp_count")),
                    "command_latency_p50_ms": _metric_number(metrics, "command_latency_p50_ms", "quote_latency_p50_ms"),
                    "command_latency_p95_ms": _metric_number(metrics, "command_latency_p95_ms", "quote_latency_p95_ms"),
                    "command_latency_p99_ms": _metric_number(metrics, "command_latency_p99_ms", "quote_latency_p99_ms"),
                    "publish_latency_p50_ms": _metric_number(metrics, "publish_latency_p50_ms"),
                    "publish_latency_p95_ms": _metric_number(metrics, "publish_latency_p95_ms"),
                    "publish_latency_p99_ms": _metric_number(metrics, "publish_latency_p99_ms"),
                    "ws_drop_count": int(_metric_number(metrics, "ws_drop_count", "ws_drops")),
                    "ws_timeout_count": int(_metric_number(metrics, "ws_timeout_count", "ws_timeouts")),
                    "cpu_pct": _metric_number(metrics, "cpu_pct", "cpu_percent"),
                    "rss_bytes": int(_metric_number(metrics, "rss_bytes", "rss")),
                    "footprint_bytes": int(_metric_number(metrics, "footprint_bytes")),
                    "queue_depth": int(_metric_number(metrics, "queue_depth", "queue_size")),
                    "queue_age_ms": _metric_number(metrics, "queue_age_ms"),
                    "kline_freshness_ms": max(0, to_millis(datetime.now(tz=UTC)) - int(runtime.sampling_metrics.get("last_kline_at_ms") or 0)),
                    "sampling_drops": int(runtime.sampling_metrics.get("sampling_drops", 0)) + int(store.metrics_snapshot().get("sampling_drops", 0)),
                    "payload": {"source": "runtime_aggregate", "run_id": runtime.run_id},
                }
                store.enqueue_minute_summary(summary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            store.enqueue_error("minute_summary", str(exc))
            await asyncio.sleep(1)


async def history_storage_guard_loop(app: FastAPI) -> None:
    if True:
        while True:
            await asyncio.sleep(60)
    await asyncio.sleep(max(0, int(settings.history_storage_startup_delay_seconds)))
    runtime: AppRuntime = app.state.runtime
    store: HistoryStore = runtime.history_store
    while True:
        try:
            quiesced = not maker_write_loop_active(app)
            result = await store.enforce_capacity(maker_quiesced=quiesced)
            if result.get("triggered") and not result.get("hard_limit_hit"):
                result["checkpoint"] = await asyncio.to_thread(store.checkpoint_if_quiesced, maker_quiesced=quiesced)
            runtime.storage_degraded = bool(result.get("hard_limit_hit"))
            app.state.history_storage_last_result = result
            await asyncio.sleep(max(10, int(settings.history_storage_guard_interval_seconds)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.storage_degraded = True
            app.state.history_storage_last_error = str(exc)
            store.enqueue_error("storage_guard", str(exc))
            await asyncio.sleep(30)


async def display_kline_persist_loop(app: FastAPI) -> None:
    """Persist synthetic public candles at aggregate cadence, never per order."""
    while True:
        try:
            if not settings.display_kline_persistence_enabled:
                await asyncio.sleep(60)
                continue
            runtime: AppRuntime = app.state.runtime
            async with SessionLocal() as session:
                markets = list(
                    (
                        await session.execute(
                            select(Market).where(Market.is_active.is_(True))
                        )
                    ).scalars()
                )
                for market in markets:
                    for interval in ("1m", "5m"):
                        await runtime.market_data.persist_display_kline(
                            session,
                            int(market.id),
                            market.symbol,
                            interval,
                        )
                await session.commit()
            await asyncio.sleep(max(1.0, settings.kline_persist_min_interval_ms / 1000))
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger("display_kline").exception("display K-line persistence loop failed")
            await asyncio.sleep(3)


async def orderbook_snapshot_loop(app: FastAPI) -> None:
    interval_seconds = ORDERBOOK_SNAPSHOT_PUSH_INTERVAL_MS / 1000
    last_pushed_content_by_symbol: dict[str, tuple] = {}
    last_pushed_published_by_symbol: dict[str, object] = {}
    last_pushed_at_by_symbol: dict[str, float] = {}
    capture_started_at: float | None = None
    while True:
        cycle_started = time.monotonic()
        try:
            runtime: AppRuntime = app.state.runtime
            symbols = await runtime.ws.subscribed_public_symbols("orderbook")
            for symbol in sorted(symbols):
                snapshot, seq, updated_at_ms = await runtime.orderbook_snapshot(symbol, ORDERBOOK_SNAPSHOT_PUSH_DEPTH)
                content = tuple((str(price), str(qty)) for price, qty in snapshot["bids"]) + tuple(
                    (str(price), str(qty)) for price, qty in snapshot["asks"]
                )
                now_ms = time.monotonic() * 1000
                same_content = last_pushed_content_by_symbol.get(symbol) == content
                if same_content and now_ms - last_pushed_at_by_symbol.get(symbol, 0.0) < ORDERBOOK_HEARTBEAT_INTERVAL_MS:
                    continue
                last_pushed_content_by_symbol[symbol] = content
                last_pushed_at_by_symbol[symbol] = now_ms
                current_published = runtime.published_orderbooks.get(symbol)
                if ORDERBOOK_CAPTURE_SECONDS > 0:
                    if capture_started_at is None:
                        capture_started_at = time.monotonic()
                    if time.monotonic() - capture_started_at <= ORDERBOOK_CAPTURE_SECONDS:
                        try:
                            with ORDERBOOK_CAPTURE_PATH.open("a", encoding="utf-8") as handle:
                                handle.write(
                                    json.dumps(
                                        {
                                            "symbol": symbol,
                                            "seq": seq,
                                            "ts": updated_at_ms,
                                            "bids": snapshot["bids"][:5],
                                            "asks": snapshot["asks"][:5],
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n"
                                )
                        except OSError:
                            pass
                previous_published = last_pushed_published_by_symbol.get(symbol)
                if same_content:
                    payload = runtime.orderbook_snapshot_payload(symbol, source="periodic_heartbeat")
                    # A Paper/Manual market can legitimately keep the same
                    # levels for a while.  Keep the public transport visibly
                    # alive without mutating the book sequence or pretending
                    # that the engine changed its content.  The websocket
                    # manager treats this explicit heartbeat as a same-seq
                    # liveness frame.
                    payload["ts"] = int(time.time() * 1000)
                    payload["heartbeat"] = True
                    payload.pop("_wire_cache", None)
                elif previous_published is not None:
                    payload = runtime.orderbook_delta_payload(
                        symbol,
                        previous_published,
                        source="periodic_delta",
                    )
                else:
                    payload = runtime.orderbook_snapshot_payload(symbol, source="periodic_snapshot")
                last_pushed_published_by_symbol[symbol] = current_published
                await runtime.ws.broadcast_public_orderbook(symbol, payload)
            elapsed = max(0.0, time.monotonic() - cycle_started)
            await asyncio.sleep(max(0.0, interval_seconds - elapsed))
        except asyncio.CancelledError:
            raise
        except Exception:
            elapsed = max(0.0, time.monotonic() - cycle_started)
            await asyncio.sleep(max(0.0, interval_seconds - elapsed))


async def contract_fast_path_reconcile_loop(app: FastAPI) -> None:
    """Periodically converge the contract fast mirror and engine with DB."""
    interval_seconds = 5.0
    while True:
        try:
            if not settings.contract_fast_path_reconcile_enabled:
                await asyncio.sleep(60)
                continue
            if is_runtime_only_mode():
                # Robot quote orders live only in memory; engine/DB drift is
                # expected by design, so pruning against DB would tear down the
                # in-memory book. Customer orders are persisted strictly and
                # reconciled by the diagnostics/slow paths instead.
                await asyncio.sleep(interval_seconds)
                continue
            runtime: AppRuntime = app.state.runtime
            contract_service = app.state.contract_service
            if not runtime.engine.books:
                await asyncio.sleep(interval_seconds)
                continue
            writer = getattr(runtime, "persistence_writer", None)
            if writer is not None:
                lag = getattr(writer, "materialization_lag", None)
                if (writer._busy or not writer._queue.empty()) or (
                    callable(lag) and lag() > 0
                ):
                    # Skip pruning while pending domain events have not been
                    # materialized; the DB view is intentionally lagging.
                    await asyncio.sleep(interval_seconds)
                    continue
            async with SessionLocal() as session:
                markets = await session.execute(
                    select(Market).where(Market.product_type == PRODUCT_TYPE_PERP)
                )
                for market in markets.scalars():
                    try:
                        async with runtime.market_locks[market.symbol]:
                            _changed_bids, _changed_asks, removed = await contract_service.reconcile_engine_book(
                                session, market
                            )
                            open_orders = await contract_service.open_order_ids_for_market(session, market)
                            mirror_keys = [
                                order_id
                                for order_id, snap in contract_service._fast_contract_orders.items()
                                if snap["symbol"] == market.symbol
                            ]
                            for order_id in mirror_keys:
                                if order_id not in open_orders:
                                    snap = contract_service._fast_contract_orders.pop(order_id, None)
                                    if snap is not None:
                                        contract_service._fast_unregister_client_id(
                                            int(snap["user_id"]),
                                            int(market.id),
                                            snap.get("client_order_id"),
                                            order_id,
                                        )
                            if removed:
                                await contract_service.broadcast_order_flow(
                                    session,
                                    market.symbol,
                                    [],
                                    set(),
                                    _changed_bids,
                                    _changed_asks,
                                    [],
                                )
                    except Exception as exc:  # pragma: no cover - defensive loop
                        logger = logging.getLogger("contract_fast_path_reconcile")
                        logger.warning("contract fast mirror reconcile failed %s: %s", market.symbol, exc)
                await session.commit()
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(interval_seconds)


async def contract_maintenance_loop(app: FastAPI) -> None:
    await asyncio.sleep(max(settings.contract_maintenance_startup_delay_seconds, 0))
    while True:
        try:
            if not settings.contract_maintenance_enabled:
                await asyncio.sleep(max(settings.contract_maintenance_interval_seconds, 1))
                continue
            async with SessionLocal() as session:
                service: ContractMaintenanceService = app.state.contract_maintenance_service
                await service.run_once(
                    session,
                    fetch_external=settings.contract_price_fetch_external and not platform_durable_contract(),
                    auto_settle_funding=settings.contract_auto_funding_enabled,
                    auto_liquidate=settings.contract_auto_liquidation_enabled,
                )
            await asyncio.sleep(max(settings.contract_maintenance_interval_seconds, 1))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


async def contract_liquidity_loop(app: FastAPI) -> None:
    while True:
        try:
            if not settings.contract_liquidity_enabled:
                await asyncio.sleep(max(settings.contract_liquidity_interval_seconds, 1))
                continue
            async with SessionLocal() as session:
                service: ContractLiquidityService = app.state.contract_liquidity_service
                await service.run_once(
                    session,
                    levels=settings.contract_liquidity_levels,
                    gap_ticks=settings.contract_liquidity_gap_ticks,
                    quantity=settings.contract_liquidity_quantity,
                )
            await asyncio.sleep(max(settings.contract_liquidity_interval_seconds, 1))
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


async def history_retention_loop(app: FastAPI) -> None:
    await asyncio.sleep(max(settings.history_retention_startup_delay_seconds, 0))
    while True:
        try:
            if history_retention_auto_enabled():
                if maker_write_loop_active(app):
                    app.state.history_retention_last_result = {
                        "status": "deferred",
                        "reason": "active_liquidity_writer",
                        "automatic_pruning": False,
                    }
                    await asyncio.sleep(max(settings.history_retention_interval_seconds, 30))
                    continue
                async with SessionLocal() as session:
                    result = await HistoryRetentionService().prune(session)
                    app.state.history_retention_last_result = result
            await asyncio.sleep(max(settings.history_retention_interval_seconds, 30))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.history_retention_last_error = str(exc)
            await asyncio.sleep(30)


async def sandbox_db_size_guard_loop(app: FastAPI) -> None:
    """Keep the local demo SQLite file from growing without bound.

    Only fires when the file already exceeds the configured cap (default 2GB),
    then prunes applied event history + old snapshot metadata. Checkpoint and
    VACUUM are deferred while liquidity writers are active because SQLite's
    database-wide maintenance lock can stall the matching/persistence path.
    This sandbox-only guard never touches customer accounting facts.
    """
    await asyncio.sleep(max(settings.sandbox_db_guard_startup_delay_seconds, 0))
    while True:
        try:
            if settings.sandbox_db_guard_enabled and database_file_path() is not None:
                if maker_write_loop_active(app):
                    app.state.sandbox_db_guard_last_result = await prune_active_history(SessionLocal)
                    await asyncio.sleep(30)
                    continue
                result = await run_sandbox_db_size_guard(SessionLocal, maker_quiesced=not maker_write_loop_active(app))
                app.state.sandbox_db_guard_last_result = result
                if result.get("triggered"):
                    logger = logging.getLogger("sandbox_db_guard")
                    logger.warning(
                        "sandbox SQLite size guard pruned events=%s snapshots=%s size=%s->%s bytes vacuum=%s",
                        result.get("deleted", {}).get("events"),
                        result.get("deleted", {}).get("snapshots"),
                        result.get("size_before_bytes"),
                        result.get("size_bytes"),
                        result.get("vacuum", {}).get("ok"),
                    )
            await asyncio.sleep(max(settings.sandbox_db_guard_interval_seconds, 60))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.sandbox_db_guard_last_error = str(exc)
            await asyncio.sleep(60)


async def financial_outbox_loop(app: FastAPI) -> None:
    dispatcher: FinancialOutboxDispatcher = app.state.financial_outbox_dispatcher
    await asyncio.sleep(max(settings.financial_outbox_startup_delay_seconds, 0))
    while True:
        try:
            result = await dispatcher.run_once()
            app.state.financial_outbox_last_result = result
            await asyncio.sleep(0.25 if result["claimed"] else 1.0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.financial_outbox_last_error = str(exc)
            await asyncio.sleep(1)


async def accounting_evidence_loop(app: FastAPI) -> None:
    """Persist one full evidence bundle per UTC date; never mutate funds."""
    await asyncio.sleep(max(settings.accounting_evidence_startup_delay_seconds, 1))
    while True:
        try:
            if settings.accounting_evidence_auto_enabled:
                if maker_write_loop_active(app):
                    app.state.accounting_evidence_last_result = {
                        "status": "deferred",
                        "reason": "active_liquidity_writer",
                        "source_of_truth_unchanged": True,
                    }
                    await asyncio.sleep(max(settings.accounting_evidence_interval_seconds, 60))
                    continue
                async with SessionLocal() as session:
                    result = await run_daily_accounting_evidence(
                        session,
                        database_url=settings.database_url,
                        stale_after_seconds=settings.accounting_evidence_stale_after_seconds,
                    )
                    app.state.accounting_evidence_last_result = result
                    app.state.accounting_evidence_last_error = None
            await asyncio.sleep(max(settings.accounting_evidence_interval_seconds, 60))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.accounting_evidence_last_error = {
                "code": exc.__class__.__name__,
                "message": str(exc)[:500],
                "at": datetime.now(tz=UTC).isoformat(),
            }
            await asyncio.sleep(max(min(settings.accounting_evidence_interval_seconds, 60), 10))


async def exchange_snapshot_loop(app: FastAPI) -> None:
    if platform_durable_contract():
        # Paper 不持久化引擎快照：报价是重启重建的临时态，快照只会在重启时
        # 恢复过期的 QuoteSet generation 并干扰新报价。
        while True:
            await asyncio.sleep(60)
    interval = max(float(settings.exchange_snapshot_interval_seconds), 1.0)
    while True:
        try:
            await asyncio.sleep(interval)
            runtime: AppRuntime = app.state.runtime
            state = runtime.exchange_core.snapshot_state()
            core_watermarks = runtime.exchange_core.watermarks_snapshot()
            writer_metrics = runtime.persistence_writer.metrics_snapshot()
            writer_watermarks = writer_metrics.get("watermarks") if isinstance(writer_metrics.get("watermarks"), dict) else {}
            critical_sink = writer_metrics.get("critical_sink") if isinstance(writer_metrics.get("critical_sink"), dict) else {}
            materialization_lag = int(writer_metrics.get("materialization_lag") or 0)
            if materialization_lag > 0 or str(critical_sink.get("status") or "HEALTHY") != "HEALTHY":
                # A snapshot that is ahead of committed SQL facts would make
                # restart recovery less trustworthy than a replay.  Wait for
                # the committed watermark instead of persisting a partial
                # engine/account state.
                continue
            snapshot_seq = min(
                int(core_watermarks.get("matched_seq", 0) or 0),
                int(writer_watermarks.get("materialized_seq") or writer_metrics.get("materialized_seq") or 0),
            )
            watermarks = {
                **core_watermarks,
                **writer_watermarks,
                "causal": runtime.causal_command_service.watermarks_snapshot()
                if runtime.causal_command_service is not None
                else {},
            }
            envelope = runtime.state_snapshot.write_snapshot(
                state,
                snapshot_seq=snapshot_seq,
                created_at_ms=to_millis(datetime.now(tz=UTC)),
                config_version="runtime",
                epoch=runtime.causal_epoch,
                watermarks=watermarks,
                committed=True,
            )
            async with SessionLocal() as session:
                session.add(
                    ExchangeSnapshotRecord(
                        snapshot_seq=envelope.snapshot_seq,
                        path=envelope.path,
                        snapshot_hash=envelope.snapshot_hash,
                        state_hash=runtime.exchange_core.state_hash(),
                        schema_version=str(envelope.schema_version),
                        epoch=envelope.epoch,
                        watermarks_json=json.dumps(envelope.watermarks or {}, ensure_ascii=False, sort_keys=True),
                        committed=envelope.committed,
                        valid=True,
                        config_version="runtime",
                        created_at=datetime.now(tz=UTC),
                    )
                )
                await session.commit()
            app.state.exchange_snapshot_last = {
                "snapshot_seq": envelope.snapshot_seq,
                "path": envelope.path,
                "snapshot_hash": envelope.snapshot_hash,
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            app.state.exchange_snapshot_last_error = str(exc)


def maker_write_loop_active(app: FastAPI) -> bool:
    """Keep non-realtime maintenance away from an active MM/FLOW writer window."""
    runtime = getattr(app.state, "runtime", None)
    if runtime is None:
        return False
    for metrics in runtime.liquidity_metrics.values():
        if not isinstance(metrics, dict):
            continue
        state = str(metrics.get("service_state") or metrics.get("status") or "").lower()
        pid = metrics.get("pid")
        if state not in {"starting", "running", "degraded", "backing_off"}:
            continue
        try:
            os.kill(int(pid), 0)
        except (TypeError, ValueError, ProcessLookupError, OSError):
            continue
        else:
            return True
    return False


async def retire_memory_mode_robot_quotes(
    session,
    order_service: OrderService,
    contract_service: ContractService,
) -> dict[str, int]:
    """Retire restart-ephemeral robot quotes without erasing real positions.

    Each live quote is canceled through the normal replay settlement path so
    SPOT frozen assets and PERP resting-order margin are released with complete
    ledger/accounting/outbox evidence. Contract account used margin is then
    reconciled to surviving isolated positions only; it is never reset to zero
    blindly.
    """
    now = datetime.now(tz=UTC)
    rows = await session.execute(
        select(Order, Market, User)
        .join(Market, Market.id == Order.market_id)
        .join(User, User.id == Order.user_id)
        .where(
            User.role == ROLE_BOT,
            Order.status.in_(["new", "partially_filled"]),
        )
        .order_by(Order.id.asc())
    )
    retired_spot = 0
    retired_contract = 0
    for order, market, user in rows.all():
        plan = {
            "now": now.isoformat(),
            "side": str(order.side),
            "changes": [],
        }
        if order.product_type == PRODUCT_TYPE_PERP:
            await contract_service.cancel_order_replay(
                session,
                user,
                market,
                str(order.order_id),
                plan,
            )
            retired_contract += 1
        elif order.product_type == PRODUCT_TYPE_SPOT:
            await order_service.cancel_order_replay(
                session,
                user,
                market,
                str(order.order_id),
                plan,
            )
            # bootstrap() can place the demo seed ladder into this fresh
            # process' engine before memory-mode retirement runs.  Replay
            # cancellation settles the durable order and its reserve, but it
            # deliberately does not mutate the in-memory book.  Evict exactly
            # the retired robot order here so a canceled seed quote cannot
            # survive as an engine-only BBO and pin the incoming Lite maker.
            # Customer orders are not selected by the ROLE_BOT query and are
            # therefore left untouched.
            order_service.runtime.engine.cancel_order(
                market.symbol,
                str(order.order_id),
            )
            retired_spot += 1

    position_rows = await session.execute(
        select(ContractPosition, Market)
        .join(Market, Market.id == ContractPosition.market_id)
        .join(User, User.id == ContractPosition.user_id)
        .where(User.role == ROLE_BOT)
    )
    position_margin: dict[tuple[int, str], Decimal] = {}
    for position, market in position_rows.all():
        key = (int(position.user_id), str(market.margin_asset or market.quote_asset))
        position_margin[key] = position_margin.get(key, ZERO) + max(
            ZERO,
            position.isolated_margin or ZERO,
        )

    accounts = (
        await session.execute(
            select(ContractAccount)
            .join(User, User.id == ContractAccount.user_id)
            .where(User.role == ROLE_BOT)
        )
    ).scalars()
    reconciled_accounts = 0
    for account in accounts:
        expected_used = position_margin.get(
            (int(account.user_id), str(account.margin_asset)),
            ZERO,
        )
        if account.used_margin == expected_used:
            continue
        before = snapshot_contract_account(account)
        account.used_margin = expected_used
        await contract_service.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="reconciliation_adjustment",
            amount=ZERO,
            before=before,
            note="memory_restart_preserve_position_margin",
            created_at=now,
        )
        reconciled_accounts += 1
    await session.flush()
    return {
        "retired_spot": retired_spot,
        "retired_contract": retired_contract,
        "reconciled_contract_accounts": reconciled_accounts,
    }


async def retire_paper_quote_orders(
    order_service: OrderService,
    contract_service: ContractService,
) -> dict[str, int]:
    """Retire restart-ephemeral PaperTrading bot orders before re-quoting.

    Paper 报价（内置 ``paperq-*`` 与外部策略 LITE ``mmv2-*`` / PERP_MM
    ``perpmm-*``）都是重启重建的临时态。旧报价订单若跨重启留在 DB 和引擎
    里，会带着过期价格继续占用盘口（幽灵订单），而新报价的 place 因
    client_order_id 幂等命中旧单无法更新价格。启动时统一撤掉全部 bot
    （role=bot）开放订单并释放保证金，报价循环/策略进程随后按当前参考价
    重新铺单。用户订单不在退役范围，FLOW 的 IOC 订单不会 resting。

    Uses short-lived per-order sessions so a failed cancel can roll back
    without expiring the lifespan bootstrap session's ORM objects.
    """
    from app.db.session import SessionLocal

    targets: list[tuple[str, int, str]] = []
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Order.order_id, Order.user_id, Order.product_type)
            .join(User, User.id == Order.user_id)
            .where(
                User.role == ROLE_BOT,
                Order.status.in_(["new", "partially_filled"]),
            )
            .order_by(Order.id.asc())
        )
        targets = [
            (str(order_id), int(user_id), str(product_type))
            for order_id, user_id, product_type in rows.all()
        ]

    retired = 0
    failed = 0
    for order_id, user_id, product_type in targets:
        async with SessionLocal() as session:
            owner = await session.get(User, user_id)
            if owner is None:
                failed += 1
                continue
            row = await session.execute(
                select(Order, Market)
                .join(Market, Market.id == Order.market_id)
                .where(Order.order_id == order_id)
            )
            record = row.first()
            if record is None:
                continue
            order, market = record
            if order.status not in {"new", "partially_filled"}:
                continue
            plan = {"side": str(order.side), "changes": [], "now": datetime.now(tz=UTC).isoformat()}
            try:
                # 引擎/序列器尚未启动时不能走正常 cancel；replay 取消只做
                # durable 释放（冻结/订单保证金），随后 load_open_orders 不会
                # 再把旧报价挂回引擎，报价循环按当前参考价重新铺单。
                if product_type == PRODUCT_TYPE_SPOT:
                    await order_service.cancel_order_replay(session, owner, market, order_id, plan)
                else:
                    await contract_service.cancel_order_replay(session, owner, market, order_id, plan)
                await session.commit()
                retired += 1
            except Exception:  # pragma: no cover - defensive startup cleanup
                await session.rollback()
                failed += 1
    logging.getLogger("paper_exchange").info(
        "retired paper bot orders retired=%s failed=%s", retired, failed
    )
    return {"retired": retired, "failed": failed}


@asynccontextmanager
async def lifespan(app: FastAPI):
    sqlite_worker_guard = SQLiteWorkerGuard(settings.database_url)
    sqlite_worker_guard.acquire()
    runtime = AppRuntime()
    history_path = Path(settings.history_db_path)
    if not history_path.is_absolute():
        history_path = ROOT_DIR / history_path
    data_root = Path(settings.sandbox_data_dir)
    if not data_root.is_absolute():
        data_root = ROOT_DIR / data_root
    runtime.history_store = HistoryStore(
        history_path,
        run_id=runtime.run_id,
        data_root=data_root,
    )
    await runtime.history_store.start()
    order_service = OrderService(runtime)
    contract_service = ContractService(runtime)
    runtime.persistence_writer = PersistenceWriter(
        SessionLocal,
        order_service,
        contract_service,
        # The unified causal journal is the replay boundary; automatic
        # deletion of old domain events would make a causal proof depend on a
        # retention race. Legacy keeps its existing sandbox retention policy.
        retention_seconds=0 if runtime.core_mode in {"unified", "shadow"} else 7 * 86400,
    )
    runtime.causal_command_service = CausalCommandService(
        SessionLocal,
        run_id=runtime.run_id,
        epoch=runtime.causal_epoch,
        mode=runtime.core_mode,
        sequence_start=settings.exchange_sequence_start,
    )
    # Read-only diagnostics may inspect the live fast mirrors without creating
    # a second source of truth; matching/accounting still remain on services.
    runtime.order_service = order_service
    runtime.contract_service = contract_service
    quote_set_service = QuoteSetService(runtime, order_service, contract_service)
    contract_price_service = ContractPriceService(runtime)
    contract_maintenance_service = ContractMaintenanceService(runtime, contract_service, contract_price_service)
    contract_liquidity_service = ContractLiquidityService(runtime, contract_service, contract_price_service)
    bot_orchestrator_service = BotOrchestratorService(runtime)
    app.state.runtime = runtime
    app.state.order_service = order_service
    app.state.contract_service = contract_service
    app.state.quote_set_service = quote_set_service
    app.state.contract_price_service = contract_price_service
    app.state.contract_maintenance_service = contract_maintenance_service
    app.state.contract_liquidity_service = contract_liquidity_service
    app.state.bot_orchestrator = bot_orchestrator_service
    runner_watch_enabled = os.getenv("SANDBOX_RUNNER_WATCHDOG_ENABLED", "0") == "1"
    runner_pid = int(os.getenv("SANDBOX_RUNNER_PID", "0") or 0)
    app.state.runner_parent_watch = {
        "enabled": runner_watch_enabled,
        "expected_pid": runner_pid if runner_watch_enabled else None,
        "status": "watching" if runner_watch_enabled else "disabled",
    }
    app.state.financial_outbox_dispatcher = FinancialOutboxDispatcher(
        SessionLocal,
        consumer_name="runtime-financial-events-v1",
        handler=lambda event: deliver_financial_runtime_event(runtime, event),
        worker_id=f"financial-outbox-{os.getpid()}",
    )
    async with SessionLocal() as session:
        await bootstrap(session, runtime)
        app.state.accounting_opening = await ensure_shadow_opening_balances(session)
        app.state.financial_outbox_checkpoint = await ensure_financial_outbox_checkpoint(session)
        await session.commit()

    # Any command that was journaled by a previous process but never reached a
    # durable execution bundle is unknown after restart.  It is queryable and
    # must not be silently replayed under a new command id.  Older deployments
    # may start before the additive migration is applied; legacy mode keeps the
    # compatibility service available while unified mode fails closed.
    try:
        app.state.causal_recovered_unknown = await runtime.causal_command_service.recover_inflight()
        app.state.causal_schema_ready = True
    except Exception as exc:
        app.state.causal_recovered_unknown = 0
        app.state.causal_schema_ready = False
        if runtime.core_mode == "unified":
            raise RuntimeError("causal chain migration is required in unified mode") from exc

    # Recover the durable view before rebuilding any in-memory book.  The
    # snapshot restores only command metadata here; the DB remains the source
    # of truth for resting orders and balances.
    await runtime.persistence_writer.materialize_catchup(timeout=120.0)
    # Paper 模式的报价是重启重建的临时态，引擎状态由 DB/journal replay 恢复；
    # 加载引擎快照会把旧 QuoteSet generation 带入新进程，错误丢弃重启后的
    # 新报价（latest-wins 判定 generation 过旧）。
    snapshot = None if (platform_durable_contract()) else runtime.state_snapshot.load_latest_valid()
    replay_since_seq = 0
    if snapshot is not None and runtime.state_snapshot.verify(snapshot):
        runtime.exchange_core.restore_state(snapshot.state, restore_books=False)
        replay_since_seq = int(snapshot.snapshot_seq)
    app.state.exchange_snapshot_loaded = (
        {
            "snapshot_seq": snapshot.snapshot_seq,
            "path": snapshot.path,
            "snapshot_hash": snapshot.snapshot_hash,
        }
        if snapshot is not None
        else {"snapshot_seq": 0, "path": None, "snapshot_hash": None, "skipped": False}
    )
    if not is_runtime_only_mode():
        app.state.exchange_commands_replayed = await runtime.persistence_writer.replay_command_state(
            runtime.exchange_core,
            since_seq=replay_since_seq,
        )

    async with SessionLocal() as session:
        app.state.legacy_liquidity_retirement = await retire_legacy_configuration(session)
        await session.commit()
        if settings.persistence_mode == "memory":
            # Robot quote orders are ephemeral: never restore them, and retire
            # any leftovers through normal financial settlement. This releases
            # quote reserves while preserving margin attached to real positions.
            app.state.memory_robot_quote_retirement = await retire_memory_mode_robot_quotes(
                session,
                order_service,
                contract_service,
            )
            await session.commit()
        markets = await session.execute(select(Market))
        market_list = list(markets.scalars())
        from app.models.paper_exchange import PaperSystemSetting
        boundaries = await session.scalars(select(PaperSystemSetting).where(PaperSystemSetting.key.like("display_history_since:%")))
        for boundary in boundaries:
            value = boundary.value_json or {}
            if value.get("symbol") and value.get("since_ms"):
                runtime.market_data.display_history_since_ms[value["symbol"]] = int(value["since_ms"])
        if True:
            for market in market_list:
                loaded = await runtime.market_data.load_from_trades(
                    session,
                    market.id,
                    market.symbol,
                    price_precision=market.price_precision,
                    qty_precision=market.qty_precision,
                )
                if not loaded:
                    await runtime.market_data.load_persisted_klines(session, market.id, market.symbol)
                await runtime.market_data.load_persisted_display_klines(session, market.id, market.symbol)
            if platform_durable_contract():
                app.state.paper_quote_retirement = await retire_paper_quote_orders(
                    order_service,
                    contract_service,
                )
            await order_service.load_open_orders(session)
            await contract_service.load_open_orders(session)
        for market in market_list:
            runtime.engine.ensure_market(market.symbol)
        app.state.sampled_markets = market_list
        for market in market_list:
            if market.product_type == "PERP":
                await contract_price_service.refresh_market_state(session, market, fetch_external=False)
        await session.commit()
        for symbol in list(runtime.engine.books.keys()):
            runtime.publish_orderbook_snapshot_unlocked(symbol)
        await order_service.load_fast_path_state(session)
        await contract_service.load_fast_path_state(session)
        await runtime.clearinghouse.load_from_db(session)
        # Durable Paper bootstrap can create its balances/accounts while the
        # initial bootstrap transaction is still being materialized.  Recheck
        # the explicit shadow opening anchor after the business facts and
        # mirrors are loaded; this remains idempotent and never repairs a
        # balance.
        app.state.accounting_opening_final = await ensure_shadow_opening_balances(session)
        await session.commit()
    from app.liquidity_map.service import LiquidityMapService
    runtime.liquidity_map = LiquidityMapService(
        runtime, SessionLocal, Path(settings.sandbox_data_dir) / "liquidity_map_history"
    )
    tasks = [
        asyncio.create_task(runtime.liquidity_map.run(), name="liquidity-map-sampler"),
        asyncio.create_task(stats_loop(app)),
        asyncio.create_task(display_kline_persist_loop(app)),
        asyncio.create_task(history_sampler_loop(app)),
        asyncio.create_task(history_minute_summary_loop(app)),
        asyncio.create_task(history_storage_guard_loop(app)),
        asyncio.create_task(orderbook_snapshot_loop(app)),
        asyncio.create_task(contract_fast_path_reconcile_loop(app)),
        asyncio.create_task(contract_maintenance_loop(app)),
        asyncio.create_task(history_retention_loop(app)),
        asyncio.create_task(sandbox_db_size_guard_loop(app)),
        asyncio.create_task(financial_outbox_loop(app)),
        asyncio.create_task(accounting_evidence_loop(app)),
        asyncio.create_task(exchange_snapshot_loop(app)),
    ]
    if runner_watch_enabled and runner_pid > 0:
        tasks.append(asyncio.create_task(runner_parent_watch_loop(app), name="runner-parent-watch"))
    if settings.contract_liquidity_enabled:
        tasks.append(asyncio.create_task(contract_liquidity_loop(app)))
    # ExchangeCore owns the single-writer command queue; without this the
    # QuoteSet worker never runs and commands are journaled but never executed.
    runtime.exchange_core.start()
    runtime.persistence_writer.start()
    from app.services.contract_ladder_service import ContractLadderService
    app.state.contract_ladder_service = ContractLadderService(runtime, contract_service, SessionLocal)
    tasks.append(asyncio.create_task(app.state.contract_ladder_service.run(), name="contract-ladder-manager"))
    from app.services.independent_flow_service import IndependentFlowService
    app.state.independent_flow = IndependentFlowService(app, SessionLocal, data_root)
    flow_url = os.environ.get("SANDBOX_FLOW_API_URL")
    if flow_url:
        tasks.append(asyncio.create_task(app.state.independent_flow.run(flow_url), name="independent-flow-supervisor"))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await runtime.persistence_writer.flush(timeout=15.0, materialize=True)
        await runtime.persistence_writer.stop(timeout=5.0)
        await runtime.exchange_core.stop()
        await runtime.stop_sequencers()
        if runtime.history_store is not None:
            runtime.history_store.enqueue_run_summary(
                {
                    "run_id": runtime.run_id,
                    "started_at_ms": to_millis(runtime.started_at),
                    "ended_at_ms": to_millis(datetime.now(tz=UTC)),
                    "persistence_mode": str(settings.persistence_mode),
                    "status": "stopped",
                    "summary": runtime.sampling_metrics,
                }
            )
            await runtime.history_store.stop(flush=True)
        sqlite_worker_guard.release()


app = FastAPI(default_response_class=ORJSONResponse, lifespan=lifespan, title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(public.router, prefix=settings.api_prefix)
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(private.router, prefix=settings.api_prefix)
app.include_router(contracts.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)
from app.api.contract_ladder import router as contract_ladder_router
app.include_router(contract_ladder_router, prefix=settings.api_prefix)
from app.api.liquidity_control import router as liquidity_control_router
app.include_router(liquidity_control_router, prefix=settings.api_prefix)
app.include_router(paper.router, prefix=settings.api_prefix)
from app.liquidity_map.api import router as liquidity_map_router
app.include_router(liquidity_map_router)
app.include_router(causal.router)

if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST_DIR / "assets"), name="frontend-assets")


@app.get("/health")
async def health():
    runtime = getattr(app.state, "runtime", None)
    writer = getattr(runtime, "persistence_writer", None) if runtime is not None else None
    if writer is None:
        return ORJSONResponse(
            status_code=503,
            content={"ok": False, "status": "STARTING", "reason": "persistence_writer_unavailable"},
        )
    metrics = writer.metrics_snapshot()
    parent_watch = getattr(app.state, "runner_parent_watch", {"enabled": False})
    parent_lost = runner_parent_watch_failed(parent_watch)
    runtime_core_mode = str(getattr(runtime, "core_mode", "legacy"))
    causal_schema_ready = bool(getattr(app.state, "causal_schema_ready", False))
    storage_degraded = bool(getattr(runtime, "storage_degraded", False))
    history_store = getattr(runtime, "history_store", None)
    storage_metrics = history_store.metrics_snapshot() if history_store is not None else {}
    healthy = (
        not bool(getattr(writer, "_stopping", False))
        and getattr(writer, "_blocked_task", None) is None
        and getattr(writer, "_critical_blocked", None) is None
        and not parent_lost
        and (runtime_core_mode != "unified" or causal_schema_ready)
    )
    payload = {
        "ok": healthy,
        "status": (
            "RUNNER_PARENT_LOST"
            if parent_lost
            else "STORAGE_DEGRADED"
            if storage_degraded
            else metrics.get("status")
        ),
        "reason": "runner_parent_lost" if parent_lost else None,
        "persistence_queue": metrics.get("queue_size", 0),
        "materialization_lag": metrics.get("materialization_lag", 0),
        "critical_sink": writer.critical_sink_status(),
        "core_mode": runtime_core_mode,
        "causal_epoch": getattr(runtime, "causal_epoch", None),
        "causal_schema_ready": causal_schema_ready,
        "causal_watermarks": (
            runtime.causal_command_service.watermarks_snapshot()
            if getattr(runtime, "causal_command_service", None) is not None
            else None
        ),
        "runner_parent_watch": parent_watch,
        "storage_degraded": storage_degraded,
        "history_storage": storage_metrics,
        **public_persistence_contract(run_id=getattr(runtime, "run_id", None), storage=storage_metrics),
    }
    if not healthy:
        return ORJSONResponse(status_code=503, content=payload)
    return payload


@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def serve_frontend(full_path: str):
    if full_path.startswith(("api/", "ws/", "docs", "redoc", "openapi.json")):
        raise HTTPException(status_code=404, detail="Not Found")
    index_file = FRONTEND_DIST_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="frontend build not found")
    return FileResponse(index_file)


@app.websocket("/ws/public")
async def ws_public(websocket: WebSocket):
    runtime: AppRuntime = websocket.app.state.runtime
    await runtime.ws.register(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("op") != "subscribe":
                continue
            channel = message["channel"]
            symbol = message["symbol"]
            interval = message.get("interval")
            if channel == "orderbook":
                depth = normalize_orderbook_depth(message.get("depth"), default=20)
                await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval, depth=depth)
                await runtime.orderbook_snapshot(symbol, depth)
                await runtime.ws.send_public_orderbook_snapshot(
                    websocket,
                    symbol,
                    runtime.orderbook_snapshot_payload(symbol, source="subscription"),
                    depth=depth,
                )
            elif channel == "trades":
                await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval)
                await websocket.send_json(
                    {
                        "channel": "trades",
                        "type": "update",
                        "symbol": symbol,
                        "items": runtime.market_data.display_recent_trade_items(symbol, 50),
                    }
                )
            elif channel == "kline":
                await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval)
                items = runtime.market_data.get_public_klines(symbol, interval or "1m", 1)
                if items:
                    await websocket.send_json(
                        {
                            "channel": "kline",
                            "type": "update",
                            "symbol": symbol,
                            "interval": interval or "1m",
                            "kline": items[-1],
                        }
                    )
            elif channel == "stats":
                await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval)
                snapshot, _, _ = await runtime.orderbook_snapshot(symbol)
                stats = runtime.market_data.compute_stats(symbol, snapshot)
                await websocket.send_json({"channel": "stats", "type": "update", "symbol": symbol, "data": stats})
            else:
                await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval)
    except WebSocketDisconnect:
        await runtime.ws.disconnect(websocket)
    except RuntimeError:
        await runtime.ws.disconnect(websocket)


@app.websocket("/ws/private")
async def ws_private(websocket: WebSocket):
    runtime: AppRuntime = websocket.app.state.runtime
    await runtime.ws.register(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("op") == "auth":
                user = None
                async with SessionLocal() as session:

                    async def cookie_paper_user() -> User | None:
                        """Paper 浏览器会话按 HttpOnly Cookie 认证（REST 同款）。"""
                        if not platform_durable_contract():
                            return None
                        raw_token = websocket.cookies.get(settings.paper_exchange_cookie_name)
                        if not raw_token:
                            return None
                        now = datetime.now(tz=UTC)
                        paper_session = await session.scalar(
                            select(PaperSession).where(
                                PaperSession.token_hash == hash_session_token(raw_token),
                                PaperSession.expires_at > now,
                            )
                        )
                        if paper_session is None:
                            return None
                        cookie_user = await session.scalar(
                            select(User).where(User.id == paper_session.user_id, User.is_active.is_(True))
                        )
                        if cookie_user is None:
                            return None
                        paper_session.last_seen_at = now
                        await session.commit()
                        return cookie_user

                    api_key = str(message.get("api_key") or "").strip()
                    cookie_authenticated = False
                    if api_key:
                        user = await session.scalar(
                            select(User).where(User.api_key == api_key, User.is_active.is_(True))
                        )
                        if user is None:
                            # 浏览器 Paper 会话本地保存的 api_key 可能已因管理员
                            # Key 轮换失效；同一 HttpOnly Cookie 会话仍有效时
                            # 回退到 Cookie 认证，避免右上角反复弹
                            # "invalid api key"。
                            user = await cookie_paper_user()
                            cookie_authenticated = user is not None
                            if user is None:
                                await websocket.send_json({"type": "error", "detail": "invalid api key"})
                                continue
                        if not cookie_authenticated:
                            timestamp = int(message.get("timestamp", 0))
                            if abs(to_millis(datetime.now(tz=UTC)) - timestamp) > settings.default_ws_signature_ttl_ms:
                                await websocket.send_json({"type": "error", "detail": "timestamp expired"})
                                continue
                            if not verify_ws_signature(str(user.api_key or ""), user.api_secret_hash or "", timestamp, message.get("signature", "")):
                                # 本地保存的 api_secret 同样可能过期；Paper 模式
                                # 下有效 Cookie 会话优先，不再报错打断浏览器用户。
                                fallback = await cookie_paper_user()
                                if fallback is None:
                                    await websocket.send_json({"type": "error", "detail": "signature invalid"})
                                    continue
                                user = fallback
                    elif platform_durable_contract():
                        # Browser Paper users authenticate over the same
                        # HttpOnly session cookie used by REST; they do not
                        # receive API secrets just to open a private socket.
                        user = await cookie_paper_user()
                        if user is None:
                            await websocket.send_json({"type": "error", "detail": "paper session invalid"})
                            continue
                    else:
                        await websocket.send_json({"type": "error", "detail": "authenticate first"})
                        continue
                await runtime.ws.auth_private(websocket, user.id)
                await websocket.send_json({"type": "auth_ok"})
            elif message.get("op") == "subscribe":
                channel = message["channel"]
                if websocket not in runtime.ws.authenticated_users:
                    await websocket.send_json({"type": "error", "detail": "authenticate first"})
                    continue
                await runtime.ws.subscribe_private(websocket, channel)
                async with SessionLocal() as session:
                    service: OrderService = websocket.app.state.order_service
                    user_id = runtime.ws.authenticated_users[websocket]
                    if channel == "balances":
                        balances = await service.serialize_balances(session, user_id)
                        await websocket.send_json({"channel": "balances", "type": "update", "data": balances})
                    elif channel == "orders":
                        snapshot_user = await session.get(User, user_id)
                        if (
                            snapshot_user is not None
                            and settings.persistence_mode == "memory"
                            and str(snapshot_user.role) == ROLE_BOT
                        ):
                            items = await service.fast_open_order_items(session, user_id)
                            state_source = "fast_mirror"
                        else:
                            rows = await session.execute(
                                select(Order, Market.symbol)
                                .join(Market, Market.id == Order.market_id)
                                .where(Order.user_id == user_id)
                                .order_by(Order.updated_at.desc())
                                .limit(20)
                            )
                            items = [
                                await service.serialize_order(session, order, symbol)
                                for order, symbol in rows.all()
                            ]
                            state_source = "database"
                        await websocket.send_json(
                            {
                                "channel": "orders",
                                "type": "snapshot",
                                "items": items,
                                "state_source": state_source,
                            }
                        )
                    elif channel == "trades":
                        rows = await session.execute(
                            select(Trade, Market)
                            .join(Market, Market.id == Trade.market_id)
                            .where((Trade.taker_user_id == user_id) | (Trade.maker_user_id == user_id))
                            .order_by(Trade.executed_at.desc())
                            .limit(50)
                        )
                        items = [
                            await service.serialize_account_trade(trade, market.symbol, user_id, market=market)
                            for trade, market in rows.all()
                        ]
                        state_source = "database"
                        await websocket.send_json(
                            {
                                "channel": "trades",
                                "type": "snapshot",
                                "items": items,
                                "state_source": state_source,
                                "durable": True,
                            }
                        )
                    elif channel == "ledger":
                        items = await service.serialize_ledger_entries(session, user_id, None, 100)
                        await websocket.send_json({"channel": "ledger", "type": "snapshot", "items": items})
                    elif channel == "contracts":
                        contract_service: ContractService = websocket.app.state.contract_service
                        account = await contract_service.serialize_account(session, user_id)
                        position_rows = await session.execute(
                            select(ContractPosition, Market)
                            .join(Market, Market.id == ContractPosition.market_id)
                            .where(ContractPosition.user_id == user_id, Market.product_type == "PERP", *ContractPosition.active_filters())
                            .order_by(Market.symbol.asc())
                        )
                        positions = [
                            await contract_service.serialize_position(position, market, session)
                            for position, market in position_rows.all()
                        ]
                        await websocket.send_json(
                            {"channel": "contracts", "type": "snapshot", "account": account, "positions": positions}
                        )
    except WebSocketDisconnect:
        await runtime.ws.disconnect(websocket)
    except RuntimeError:
        await runtime.ws.disconnect(websocket)
