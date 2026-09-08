from __future__ import annotations

import math
import os
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import PRODUCT_TYPE_PERP, ZERO
from app.core.config import settings
from app.core.decimal_utils import decimal_to_str, quantize_scale, to_decimal
from app.core.time_utils import to_millis
from app.db.session import get_db_session
from app.models.market import Market
from app.models.market_maker_instance import MarketMakerInstance
from app.models.trade import Trade
from app.schemas.api import MarketItem, MarketListResponse
from app.services.market_data_service import BOOTSTRAP_SEED_SOURCE
from app.services.process_status import pid_is_running as cached_pid_is_running
from app.services.persistence_contract import (
    is_runtime_only_mode,

    paper_product_enabled,
    platform_durable_contract,
    public_persistence_contract,
)

router = APIRouter(tags=["public"])
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MAKER_RUNTIME_DIR = PROJECT_ROOT / ".runtime"
MAKER_HEARTBEAT_STALE_SECONDS = 45
PERP_LAST_TRADE_MAX_AGE_SECONDS = 60


def get_runtime(request: Request):
    return request.app.state.runtime


def normalize_market_number(value, scale: int):
    return quantize_scale(value, scale)


def _decimal(value: str | int | float | Decimal | None) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    return Decimal(str(value))


def _maker_reference_price_payload(
    metrics: dict,
    *,
    product_type: str,
    now_ms: int,
) -> dict | None:
    """Expose the maker's authoritative Binance reference without leaking config.

    The maker metrics are already the source used by the quote planner.  Returning
    that exact source lets the UI compare the local book with the price that is
    actually driving quotes, instead of making a second unrelated Binance request.
    """
    if not isinstance(metrics, dict):
        return None
    is_perp = str(product_type or "").upper() == PRODUCT_TYPE_PERP
    bid = metrics.get("binance_bid_price") if is_perp else metrics.get("external_bid")
    ask = metrics.get("binance_ask_price") if is_perp else metrics.get("external_ask")
    mid = metrics.get("binance_mid_price") if is_perp else metrics.get("external_mid")
    if mid in (None, ""):
        bid_value = _decimal(bid)
        ask_value = _decimal(ask)
        if bid_value > ZERO and ask_value > ZERO:
            mid = decimal_to_str((bid_value + ask_value) / Decimal("2"))
    if mid in (None, ""):
        return None

    health = metrics.get("health") if isinstance(metrics.get("health"), dict) else {}
    runtime_config = metrics.get("runtime_config") if isinstance(metrics.get("runtime_config"), dict) else {}
    metrics_ts = int(metrics.get("ts") or 0)
    source_age_ms = int(
        (
            metrics.get("binance_bbo_age_ms")
            if is_perp
            else metrics.get("reference_age_ms", health.get("external_stale_ms"))
        )
        or 0
    )
    age_ms = max(0, source_age_ms + max(0, now_ms - metrics_ts)) if metrics_ts > 0 else source_age_ms
    explicit_updated_at = int(metrics.get("reference_updated_at_ms") or 0)
    if explicit_updated_at > 0:
        age_ms = max(0, now_ms - explicit_updated_at)
    stale_after_ms = int(
        metrics.get("reference_stale_after_ms")
        or (
            runtime_config.get("binance_bbo_ws_stale_ms")
            if is_perp
            else runtime_config.get("external_stale_ms")
        )
        or (1_000 if is_perp else 1_000)
    )
    return {
        "venue": "Binance",
        "market": "perpetual" if is_perp else "spot",
        "bid": str(bid) if bid not in (None, "") else None,
        "ask": str(ask) if ask not in (None, "") else None,
        "mid": str(mid),
        "source": str(metrics.get("binance_bbo_source") or metrics.get("reference_source") or "ws"),
        "age_ms": age_ms,
        "updated_at": now_ms - age_ms,
        "stale_after_ms": stale_after_ms,
        "stale": bool(metrics.get("reference_status") == "stale") or age_ms > stale_after_ms,
        "status": str(metrics.get("reference_status") or ("stale" if age_ms > stale_after_ms else "fresh")),
        "stale_quote_action": metrics.get("stale_quote_action"),
    }


def _user_order_interaction_metrics(runtime, symbol: str, product_type: str) -> dict:
    """Estimate how much current user residual is covered by bot liquidity.

    This is a read-only operational metric. It intentionally counts only
    strategy-owned opposite orders as coverage, so a customer order cannot be
    used as the reference price or as fake FLOW-only liquidity.
    """
    if str(product_type).upper() == PRODUCT_TYPE_PERP:
        service = getattr(runtime, "contract_service", None)
        source = getattr(service, "_fast_contract_orders", {}) if service is not None else {}
    else:
        service = getattr(runtime, "order_service", None)
        source = getattr(service, "_fast_orders", {}) if service is not None else {}
    symbol_upper = str(symbol).upper()

    def is_bot_snapshot(snap: dict) -> bool:
        order_id = str(snap.get("order_id") or "")
        client_id = str(snap.get("client_order_id") or "")
        return order_id.startswith(("qord_", "mmv2-", "perpmm-", "flowv2-", "perpflow-")) or client_id.startswith(
            ("mmv2-", "perpmm-", "flowv2-", "perpflow-")
        )

    live = [
        snap
        for snap in source.values()
        if isinstance(snap, dict)
        and str(snap.get("symbol") or "").upper() == symbol_upper
        and str(snap.get("status") or "") in {"new", "partially_filled"}
        and Decimal(str(snap.get("remaining_quantity") or snap.get("quantity") or "0")) > ZERO
    ]
    users = [snap for snap in live if not is_bot_snapshot(snap)]
    bots = [snap for snap in live if is_bot_snapshot(snap)]
    residual = Decimal("0")
    covered = Decimal("0")
    estimated_cycles = 0
    for user_order in users:
        remaining = Decimal(str(user_order.get("remaining_quantity") or user_order.get("quantity") or "0"))
        price = Decimal(str(user_order.get("price") or "0"))
        side = str(user_order.get("side") or "").lower()
        if remaining <= ZERO or price <= ZERO or side not in {"buy", "sell"}:
            continue
        opposite = "sell" if side == "buy" else "buy"
        available = Decimal("0")
        for bot_order in bots:
            if str(bot_order.get("side") or "").lower() != opposite:
                continue
            bot_price = Decimal(str(bot_order.get("price") or "0"))
            if (side == "buy" and bot_price <= price) or (side == "sell" and bot_price >= price):
                available += Decimal(str(bot_order.get("remaining_quantity") or bot_order.get("quantity") or "0"))
        residual += remaining
        covered_for_order = min(remaining, available)
        covered += covered_for_order
        if covered_for_order > ZERO:
            estimated_cycles = max(estimated_cycles, math.ceil(float(remaining / covered_for_order)))
    return {
        "user_open_order_count": len(users),
        "user_residual_quantity": decimal_to_str(residual),
        "user_residual_coverage_quantity": decimal_to_str(covered),
        "user_residual_uncovered_quantity": decimal_to_str(max(ZERO, residual - covered)),
        "estimated_cleanup_cycles": estimated_cycles,
        "head_refresh_interval_ms": int(settings.quote_head_interval_ms),
        "coverage_source": "bot_opposite_quotes_only",
    }


def _fresh_recent_trade_price(runtime, symbol: str, *, max_age_seconds: int | None = None) -> Decimal:
    trades = runtime.market_data.recent_trade_items(symbol, 1)
    if not trades:
        return ZERO
    trade = trades[0]
    ts = int(trade.get("ts") or 0)
    if max_age_seconds is not None and ts > 0:
        age_seconds = (to_millis(datetime.now(tz=UTC)) - ts) / 1000
        if age_seconds > max_age_seconds:
            return ZERO
    return to_decimal(trade.get("price"))


def _perp_display_price(runtime, symbol: str, stats: dict) -> Decimal:
    trade_price = _fresh_recent_trade_price(runtime, symbol, max_age_seconds=PERP_LAST_TRADE_MAX_AGE_SECONDS)
    if trade_price > ZERO:
        return trade_price
    snapshot = runtime.contract_price_snapshots.get(symbol.upper(), {})
    for key in ("mark_price", "local_mid_price", "index_price"):
        price = to_decimal(snapshot.get(key))
        if price > ZERO:
            return price
    return to_decimal(stats.get("mid_price"))


def _market_display_price(market: Market, runtime, stats: dict) -> Decimal:
    if market.product_type == PRODUCT_TYPE_PERP:
        return _perp_display_price(runtime, market.symbol, stats)
    # SPOT 同样只在最近成交仍新鲜时用成交价，否则回退盘口 mid，
    # 避免价格源切换后陈旧的 last_price 一直停留在旧价位。
    trade_price = _fresh_recent_trade_price(runtime, market.symbol, max_age_seconds=PERP_LAST_TRADE_MAX_AGE_SECONDS)
    return trade_price if trade_price > ZERO else to_decimal(stats.get("mid_price"))


def _kline_source_counts(items: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        source_counts = item.get("source_counts")
        if isinstance(source_counts, dict):
            for source, count in source_counts.items():
                try:
                    numeric_count = int(count or 0)
                except (TypeError, ValueError):
                    numeric_count = 0
                if numeric_count > 0:
                    counts[str(source)] = counts.get(str(source), 0) + numeric_count
            continue
        source = str(item.get("source") or "unknown")
        try:
            trade_count = int(item.get("trade_count") or 0)
        except (TypeError, ValueError):
            trade_count = 0
        counts[source] = counts.get(source, 0) + max(trade_count, 1)
    return counts


def _kline_source_decimal_totals(items: list[dict], field: str, fallback_total_field: str) -> dict[str, str]:
    totals: dict[str, Decimal] = {}
    for item in items:
        source_values = item.get(field)
        if isinstance(source_values, dict):
            for source, value in source_values.items():
                amount = _decimal(value)
                if amount > ZERO:
                    key = str(source)
                    totals[key] = totals.get(key, ZERO) + amount
            continue
        source = str(item.get("source") or "unknown")
        amount = _decimal(item.get(fallback_total_field))
        if amount > ZERO:
            totals[source] = totals.get(source, ZERO) + amount
    return {
        source: decimal_to_str(amount)
        for source, amount in totals.items()
        if amount > ZERO
    }


def _kline_meta(
    *,
    symbol: str,
    interval: str,
    limit: int,
    include_seed: bool,
    items: list[dict],
    hidden_seed_items: list[dict] | None = None,
    maker_instance: dict | None = None,
    persistence: dict | None = None,
) -> dict:
    hidden_seed_count = len(hidden_seed_items or [])
    source_counts = _kline_source_counts(items)
    quality = _kline_quality(items)
    maker_running = bool(maker_instance and maker_instance.get("running"))
    if items and any(item.get("sampled") for item in items):
        status = "sampled"
        message = "K 线来自每秒价格采样与闭合低频聚合，仅用于沙盒展示，不代表商用真实成交 K 线。"
    elif items and quality["status"] == "low_quality":
        status = "low_quality"
        message = "K 线呈机械重复或同价竖线形态，当前不作为正常做市图表；请检查机器人运行状态和成交生成逻辑。"
    elif items:
        status = "ok"
        message = "K 线来自成交聚合。"
    elif hidden_seed_count > 0:
        status = "seed_hidden"
        message = "仅检测到初始化历史 K 线，当前默认隐藏；打开初始化历史可查看，但不作为真实做市数据。"
    elif maker_instance is not None and not maker_running:
        status = "waiting_for_instance"
        message = "当前无运行中做市实例；K 线只会在机器人或真实成交产生后更新，初始化历史默认隐藏。"
    else:
        status = "empty"
        message = "暂无真实/机器人成交 K 线；机器人运行并产生成交后会自动生成。"
    return {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "include_seed": include_seed,
        "count": len(items),
        "data_status": status,
        "message": message,
        "source_counts": source_counts,
        "source_volumes": _kline_source_decimal_totals(items, "source_volumes", "volume"),
        "source_quote_volumes": _kline_source_decimal_totals(items, "source_quote_volumes", "quote_volume"),
        "hidden_seed_count": hidden_seed_count,
        "quality": quality,
        "maker_instance": {
            "status": maker_instance.get("status"),
            "running": maker_running,
            "heartbeat_status": maker_instance.get("heartbeat_status"),
            "strategy_version": maker_instance.get("strategy_version"),
        }
        if maker_instance is not None
        else None,
        "persistence": persistence,
    }


def _kline_item_has_seed(item: dict) -> bool:
    source_counts = item.get("source_counts")
    seed_count = 0
    if isinstance(source_counts, dict):
        try:
            seed_count = int(source_counts.get(BOOTSTRAP_SEED_SOURCE) or 0)
        except (TypeError, ValueError):
            seed_count = 0
    return item.get("source") == BOOTSTRAP_SEED_SOURCE or seed_count > 0


def _kline_quality(items: list[dict]) -> dict:
    sample_count = len(items)
    if sample_count == 0:
        return {
            "status": "empty",
            "sample_count": 0,
            "flat_ohlc_count": 0,
            "zero_body_count": 0,
            "unique_close_count": 0,
            "flat_ohlc_ratio": 0,
            "zero_body_ratio": 0,
            "price_range": "0",
            "reason": "no kline samples",
        }

    flat_ohlc_count = 0
    zero_body_count = 0
    highs: list[Decimal] = []
    lows: list[Decimal] = []
    closes: set[Decimal] = set()
    for item in items:
        open_price = _decimal(item.get("open"))
        high_price = _decimal(item.get("high"))
        low_price = _decimal(item.get("low"))
        close_price = _decimal(item.get("close"))
        highs.append(high_price)
        lows.append(low_price)
        closes.add(close_price)
        if open_price == high_price == low_price == close_price:
            flat_ohlc_count += 1
        if open_price == close_price:
            zero_body_count += 1

    flat_ohlc_ratio = flat_ohlc_count / sample_count
    zero_body_ratio = zero_body_count / sample_count
    unique_close_count = len(closes)
    price_range = (max(highs) - min(lows)) if highs and lows else Decimal("0")
    low_quality = bool(
        sample_count >= 30
        and (
            (flat_ohlc_ratio >= 0.85 and unique_close_count <= 3)
            or (zero_body_ratio >= 0.95 and unique_close_count <= 2)
            or price_range <= Decimal("0")
        )
    )
    reason = (
        "many flat/repeated candles; likely mechanical or dirty generated data"
        if low_quality
        else "kline samples passed basic variation checks"
    )
    return {
        "status": "low_quality" if low_quality else "ok",
        "sample_count": sample_count,
        "flat_ohlc_count": flat_ohlc_count,
        "zero_body_count": zero_body_count,
        "unique_close_count": unique_close_count,
        "flat_ohlc_ratio": round(flat_ohlc_ratio, 4),
        "zero_body_ratio": round(zero_body_ratio, 4),
        "price_range": decimal_to_str(price_range),
        "reason": reason,
    }


def _append_check(checks: list[dict], code: str, label: str, severity: str, detail: str) -> None:
    checks.append({"code": code, "label": label, "severity": severity, "detail": detail})


def _overall_status(checks: list[dict]) -> str:
    severities = {item["severity"] for item in checks}
    if "critical" in severities:
        return "critical"
    if "warn" in severities:
        return "warn"
    return "ok"


def _maker_instance_pid_path(symbol: str) -> Path:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum() or ch in {"_", "-"})
    return MAKER_RUNTIME_DIR / f"mm_service_{safe_symbol}.pid"


def _read_pid(path: Path) -> int | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def _pid_is_running(pid: int | None) -> bool:
    return cached_pid_is_running(pid)


def _heartbeat_status(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "missing"
    current = now or datetime.now(tz=UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if (current - value).total_seconds() > MAKER_HEARTBEAT_STALE_SECONDS:
        return "stale"
    return "ok"


def _public_maker_status(market: Market, request: Request, instance: MarketMakerInstance | None) -> dict:
    pid_path = _maker_instance_pid_path(market.symbol)
    pid = _read_pid(pid_path)
    if pid is None and instance is not None:
        pid = instance.pid
    running = _pid_is_running(pid)
    runtime_metrics = request.app.state.runtime.liquidity_metrics.get(market.symbol.upper(), {})
    persisted_metrics = instance.last_metrics_json if instance is not None and isinstance(instance.last_metrics_json, dict) else {}
    metrics = runtime_metrics or persisted_metrics
    now = datetime.now(tz=UTC)
    persisted_status = instance.status if instance is not None else "stopped"
    last_heartbeat_at = instance.last_heartbeat_at if instance is not None else None
    hb_status = _heartbeat_status(last_heartbeat_at, now=now)
    started_at = instance.started_at if instance is not None else None
    starting_grace = (
        running
        and last_heartbeat_at is None
        and started_at is not None
        and ((now - (started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at)).total_seconds() <= MAKER_HEARTBEAT_STALE_SECONDS)
    )
    if running:
        if persisted_status == "starting" and starting_grace:
            status = "starting"
        elif hb_status == "stale" or (hb_status == "missing" and not starting_grace):
            status = "stale"
        else:
            status = "running"
    elif pid is not None:
        status = "stale"
    elif persisted_status in {"starting", "running", "stopping"}:
        status = "stale"
    else:
        status = persisted_status or "stopped"
    last_metrics_ts = metrics.get("ts") if isinstance(metrics, dict) else None
    metrics_age_ms: int | None = None
    if last_metrics_ts is not None:
        try:
            metrics_age_ms = max(0, to_millis(now) - int(last_metrics_ts))
        except (TypeError, ValueError):
            metrics_age_ms = None
    metrics_data_plane_status = (
        "missing"
        if last_metrics_ts is None or metrics_age_ms is None
        else (
            "stale"
            if metrics_age_ms > MAKER_HEARTBEAT_STALE_SECONDS * 1000
            else "fresh"
        )
    )
    engine_open_order_count: int | None = None
    try:
        engine_book = request.app.state.runtime.engine.books.get(market.symbol)
        engine_open_order_count = len(engine_book.orders) if engine_book is not None else 0
    except (AttributeError, TypeError):
        engine_open_order_count = None
    data_plane_status = (
        "empty"
        if running and engine_open_order_count == 0
        else metrics_data_plane_status
    )
    if running and status == "running" and data_plane_status != "fresh":
        status = "degraded"
    runtime_strategy = (
        (instance.strategy_key if instance is not None else None)
        or request.app.state.runtime.get_liquidity_strategy_selection(market.symbol)
    )
    configured_strategy = request.app.state.runtime.get_liquidity_strategy_selection(market.symbol)
    runtime_strategy_key = str(runtime_strategy or "").upper()
    configured_strategy_key = str(configured_strategy or "").upper()
    now_ms = to_millis(now)
    runner_watch = getattr(request.app.state, "runner_parent_watch", {"enabled": False})
    runner_watch_public = {
        "enabled": bool(runner_watch.get("enabled")) if isinstance(runner_watch, dict) else False,
        "status": runner_watch.get("status") if isinstance(runner_watch, dict) else None,
        "expected_pid": runner_watch.get("expected_pid") if isinstance(runner_watch, dict) else None,
        "actual_parent_pid": runner_watch.get("actual_parent_pid") if isinstance(runner_watch, dict) else None,
        "detected_at": runner_watch.get("detected_at") if isinstance(runner_watch, dict) else None,
    }
    runner_parent_lost = bool(
        runner_watch_public["enabled"] and runner_watch_public["status"] == "parent_lost"
    )
    if runner_parent_lost:
        status = "error"
    return {
        "symbol": market.symbol,
        "status": status,
        "persisted_status": persisted_status,
        "heartbeat_status": hb_status,
        "running": running,
        "strategy_version": runtime_strategy,
        "configured_strategy_version": configured_strategy,
        "runtime_strategy_mismatch": bool(
            running
            and runtime_strategy_key
            and configured_strategy_key
            and runtime_strategy_key != configured_strategy_key
        ),
        "metrics_present": bool(metrics),
        "last_metrics_ts": last_metrics_ts,
        "metrics_age_ms": metrics_age_ms,
        "data_plane_status": data_plane_status,
        "engine_open_order_count": engine_open_order_count,
        "last_heartbeat_at": to_millis(last_heartbeat_at) if last_heartbeat_at is not None else None,
        "started_at": to_millis(instance.started_at) if instance is not None and instance.started_at is not None else None,
        "stopped_at": to_millis(instance.stopped_at) if instance is not None and instance.stopped_at is not None else None,
        "last_error": (
            "runner parent lost; maker processes were stopped"
            if runner_parent_lost
            else instance.last_error if instance is not None else None
        ),
        "runner_parent_watch": runner_watch_public,
        "reference_price": _maker_reference_price_payload(
            metrics,
            product_type=market.product_type,
            now_ms=now_ms,
        ),
    }


async def build_market_health(market: Market, request: Request, session: AsyncSession) -> dict:
    runtime = get_runtime(request)
    orderbook, _, _ = await runtime.orderbook_snapshot(market.symbol, 100)
    stats = runtime.market_data.compute_stats(market.symbol, orderbook)
    last_price = _market_display_price(market, runtime, stats)
    rolling_24h = await runtime.market_data.compute_24h_stats(session, market.id, last_price)

    bids = orderbook["bids"]
    asks = orderbook["asks"]
    best_bid = _decimal(bids[0][0] if bids else None)
    best_ask = _decimal(asks[0][0] if asks else None)
    spread_pct = _decimal(stats.get("spread_pct"))
    book_imbalance = _decimal(stats.get("book_imbalance"))
    depth_0_5pct = _decimal(stats.get("depth_amount_0_5pct"))
    depth_2pct = _decimal(stats.get("depth_amount_2pct"))
    quote_volume_24h = _decimal(rolling_24h.get("quote_volume_24h"))
    checks: list[dict] = []

    if not market.is_active:
        _append_check(checks, "market_paused", "市场暂停", "critical", "该市场已暂停，新的下单请求会被拒绝。")
    if not bids:
        _append_check(checks, "empty_bid", "买盘为空", "critical", "当前没有买盘，卖出市价单无法正常成交。")
    if not asks:
        _append_check(checks, "empty_ask", "卖盘为空", "critical", "当前没有卖盘，买入市价单无法正常成交。")
    if bids and asks and best_bid >= best_ask:
        _append_check(checks, "crossed_book", "盘口交叉", "critical", "买一价格不低于卖一价格，需要检查挂单或撮合状态。")

    if bids and asks:
        if spread_pct > Decimal("2"):
            _append_check(checks, "very_wide_spread", "点差极宽", "critical", f"当前点差约 {decimal_to_str(spread_pct)}%，容易被操作手打穿。")
        elif spread_pct > Decimal("0.5"):
            _append_check(checks, "wide_spread", "点差偏宽", "warn", f"当前点差约 {decimal_to_str(spread_pct)}%，做市参数可能需要收紧。")

    thin_depth_threshold = max(_decimal(market.min_notional) * Decimal("20"), Decimal("1000"))
    if bids and asks and depth_0_5pct < thin_depth_threshold:
        _append_check(
            checks,
            "thin_depth",
            "近端深度偏薄",
            "warn",
            f"0.5% 范围内双边深度约 {decimal_to_str(depth_0_5pct)} {market.quote_asset}，低于 {decimal_to_str(thin_depth_threshold)} {market.quote_asset}。",
        )

    if bids and asks and (book_imbalance <= Decimal("0.05") or book_imbalance >= Decimal("0.95")):
        _append_check(checks, "severe_imbalance", "买卖盘极度失衡", "critical", f"前 10 档买盘占比约 {decimal_to_str(book_imbalance * Decimal('100'))}%。")
    elif bids and asks and (book_imbalance <= Decimal("0.15") or book_imbalance >= Decimal("0.85")):
        _append_check(checks, "book_imbalance", "买卖盘失衡", "warn", f"前 10 档买盘占比约 {decimal_to_str(book_imbalance * Decimal('100'))}%。")

    if quote_volume_24h <= Decimal("0"):
        _append_check(checks, "no_24h_turnover", "24H 无成交额", "warn", "最近 24 小时没有成交额，K 线和成交参考不足。")

    if not checks:
        _append_check(checks, "normal", "结构正常", "ok", "当前未发现空边、交叉、极端点差或明显深度异常。")

    return {
        "symbol": market.symbol,
        "status": _overall_status(checks),
        "market_type": market.market_type,
        "is_active": market.is_active,
        "ts": stats["ts"],
        "metrics": {
            "best_bid": stats["best_bid"],
            "best_ask": stats["best_ask"],
            "mid_price": stats["mid_price"],
            "spread": stats["spread"],
            "spread_pct": stats["spread_pct"],
            "depth_amount_0_5pct": stats["depth_amount_0_5pct"],
            "depth_amount_2pct": stats["depth_amount_2pct"],
            "book_imbalance": stats["book_imbalance"],
            "bid_level_count": len(bids),
            "ask_level_count": len(asks),
            "quote_volume_24h": rolling_24h["quote_volume_24h"],
            "user_order_interaction": _user_order_interaction_metrics(runtime, market.symbol, market.product_type),
        },
        "checks": checks,
        "persistence": public_persistence_contract(run_id=getattr(runtime, "run_id", None)),
    }


@router.get("/markets/maker-status")
async def market_maker_status_catalog(request: Request, session: AsyncSession = Depends(get_db_session)):
    """Public enabled badges only; never expose strategy configuration or accounts."""
    service = getattr(request.app.state, "contract_ladder_service", None)
    records = getattr(service, "records", {})
    markets = (await session.scalars(select(Market).order_by(Market.symbol))).all()
    instances = {item.market_id: item for item in (await session.scalars(select(MarketMakerInstance))).all()}
    return {"items": [{"symbol": market.symbol, "enabled": bool(records[market.symbol].get("config", {}).get("enabled"))
        if market.symbol in records else bool(_public_maker_status(market, request, instances.get(market.id)).get("running"))}
        for market in markets]}


@router.get("/markets", response_model=MarketListResponse)
async def list_markets(request: Request, session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Market).order_by(Market.symbol.asc()))
    items = [
        MarketItem(
            symbol=market.symbol,
            product_type=market.product_type,
            market_type=market.market_type,
            base_asset=market.base_asset,
            quote_asset=market.quote_asset,
            margin_asset=market.margin_asset,
            price_tick=normalize_market_number(market.price_tick, market.price_precision),
            qty_step=normalize_market_number(market.qty_step, market.qty_precision),
            min_qty=normalize_market_number(market.min_qty, market.qty_precision),
            min_notional=market.min_notional,
            max_leverage=market.max_leverage,
            default_leverage=market.default_leverage,
            maintenance_margin_rate=market.maintenance_margin_rate,
            funding_rate=market.funding_rate,
            funding_interval_hours=market.funding_interval_hours,
            index_price_source=market.index_price_source,
            mark_price_mode=market.mark_price_mode,
            funding_rate_mode=market.funding_rate_mode,
            funding_interest_rate=market.funding_interest_rate,
            funding_clamp_rate=market.funding_clamp_rate,
            funding_cap_rate=market.funding_cap_rate,
            funding_impact_notional=market.funding_impact_notional,
            contract_trading_mode=market.contract_trading_mode,
            reference_price=quantize_scale(market.reference_price, market.price_precision)
            if market.reference_price is not None
            else None,
            price_precision=market.price_precision,
            qty_precision=market.qty_precision,
            is_active=market.is_active,
        )
        for market in result.scalars()
    ]
    return MarketListResponse(
        items=items,
        persistence=public_persistence_contract(run_id=getattr(get_runtime(request), "run_id", None)),
    )


@router.get("/markets/{symbol}/ticker")
async def get_ticker(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    symbol = symbol.upper()
    runtime = get_runtime(request)
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    orderbook, _, _ = await runtime.orderbook_snapshot(symbol)
    stats = runtime.market_data.compute_stats(symbol, orderbook)
    last_price = _market_display_price(market, runtime, stats)
    rolling_24h = await runtime.market_data.compute_24h_stats(session, market.id, last_price)
    payload = {
        "symbol": symbol,
        "product_type": market.product_type,
        "last_price": decimal_to_str(last_price),
        "price_source": "mark_or_mid" if market.product_type == PRODUCT_TYPE_PERP else "trade_or_mid",
        "updated_at": stats["ts"],
        "best_bid": stats["best_bid"],
        "best_ask": stats["best_ask"],
        "spread": stats["spread"],
        "spread_pct": stats["spread_pct"],
        "mid_price": stats["mid_price"],
        "depth_amount_0_5pct": stats["depth_amount_0_5pct"],
        "depth_amount_2pct": stats["depth_amount_2pct"],
        "open_24h": rolling_24h["open_24h"],
        "high_24h": rolling_24h["high_24h"],
        "low_24h": rolling_24h["low_24h"],
        "change_24h": rolling_24h["change_24h"],
        "change_24h_pct": rolling_24h["change_24h_pct"],
        "volume_24h": rolling_24h["volume_24h"],
        "quote_volume_24h": rolling_24h["quote_volume_24h"],
        "is_active": market.is_active,
    }
    payload["persistence"] = public_persistence_contract(run_id=getattr(runtime, "run_id", None))
    return payload


@router.get("/markets/{symbol}/orderbook")
async def get_orderbook(symbol: str, request: Request, depth: int = Query(default=20, ge=1, le=100)):
    symbol = symbol.upper()
    runtime = get_runtime(request)
    orderbook, seq, updated_at_ms = await runtime.orderbook_snapshot(symbol, depth)
    return {
        "symbol": symbol,
        "stream_id": runtime.orderbook_stream_id,
        "stream_epoch": getattr(runtime, "causal_epoch", None),
        "last_update_id": seq,
        "bids": orderbook["bids"],
        "asks": orderbook["asks"],
        "ts": updated_at_ms,
    }


@router.get("/markets/{symbol}/trades")
async def get_trades(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    limit: int = Query(default=100, ge=1, le=200),
    include_seed: bool = Query(default=False),
):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    conditions = [Trade.market_id == market.id]
    cutoff = get_runtime(request).market_data.display_history_since_ms.get(symbol, 0)
    if cutoff:
        conditions.append(Trade.executed_at > datetime.fromtimestamp(cutoff / 1000, tz=UTC))
    if not include_seed:
        conditions.append(Trade.source != BOOTSTRAP_SEED_SOURCE)
    runtime_items: list[dict] = []
    if is_runtime_only_mode() or platform_durable_contract():
        runtime = get_runtime(request)
        seen_runtime_ids: set[str] = set()
        for item in runtime.market_data.display_recent_trade_items(
            symbol,
            200,
            include_seed=include_seed,
        ):
            runtime_trade_id = str(item.get("trade_id") or "")
            if runtime_trade_id and runtime_trade_id in seen_runtime_ids:
                continue
            if runtime_trade_id:
                seen_runtime_ids.add(runtime_trade_id)
            public_item = {
                    "trade_id": item.get("trade_id"),
                    "price": decimal_to_str(
                        quantize_scale(item.get("price"), market.price_precision)
                    ),
                    "quantity": decimal_to_str(
                        quantize_scale(item.get("quantity"), market.qty_precision)
                    ),
                    "side": item.get("side"),
                    "product_type": market.product_type,
                    "source": item.get("source"),
                    "ts": int(item.get("ts") or 0),
                }
            if item.get("synthetic") is True:
                public_item.update(
                    {
                        "synthetic": True,
                        "execution_mode": item.get("execution_mode"),
                        "durable": False,
                        "financial_effect": False,
                        "persistence": item.get("persistence"),
                    }
                )
            runtime_items.append(public_item)
    db_limit = limit + len(runtime_items) if runtime_items else limit
    rows = await session.execute(
        select(Trade).where(*conditions).order_by(Trade.executed_at.desc()).limit(db_limit)
    )
    db_items = [
        {
            "trade_id": trade.trade_id,
            "price": decimal_to_str(quantize_scale(trade.price, market.price_precision)),
            "quantity": decimal_to_str(quantize_scale(trade.quantity, market.qty_precision)),
            "side": trade.taker_side,
            "product_type": trade.product_type,
            "source": trade.source,
            "ts": to_millis(trade.executed_at),
        }
        for trade in rows.scalars()
    ]
    if not runtime_items:
        return {"items": db_items}

    # Live fast-path trades and their later materialized DB rows can have
    # different trade ids.  Pair identical executions across the two sources
    # one-for-one so legitimate repeated fills keep their multiplicity.
    def execution_key(item: dict) -> tuple:
        return (
            int(item.get("ts") or 0),
            str(item.get("price") or ""),
            str(item.get("quantity") or ""),
            str(item.get("side") or ""),
            str(item.get("product_type") or ""),
            str(item.get("source") or ""),
        )

    runtime_ids = {
        str(item["trade_id"])
        for item in runtime_items
        if item.get("trade_id")
    }
    unmatched_runtime = Counter(execution_key(item) for item in runtime_items)
    merged: list[tuple[dict, int, int]] = [
        (item, 0, index) for index, item in enumerate(runtime_items)
    ]
    for index, item in enumerate(db_items):
        key = execution_key(item)
        trade_id = str(item.get("trade_id") or "")
        if trade_id and trade_id in runtime_ids:
            if unmatched_runtime[key] > 0:
                unmatched_runtime[key] -= 1
            continue
        if unmatched_runtime[key] > 0:
            unmatched_runtime[key] -= 1
            continue
        merged.append((item, 1, index))
    merged.sort(key=lambda entry: (-int(entry[0].get("ts") or 0), entry[1], entry[2]))
    return {"items": [item for item, _origin, _index in merged[:limit]]}


@router.get("/markets/{symbol}/klines")
async def get_klines(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    interval: str = Query(default="1m"),
    limit: int = Query(default=200, ge=1, le=500),
    include_seed: bool = Query(default=False),
):
    symbol = symbol.upper()
    runtime = get_runtime(request)
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    # PaperTrading exposes its own runtime maker status under /paper.  Do not
    # attach the legacy durable MM row here, otherwise a healthy Paper book
    # can be reported as the stale production-style instance in K-line meta.
    instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
    maker_instance = None if paper_product_enabled() else _public_maker_status(market, request, instance)
    items = runtime.market_data.get_public_klines(symbol, interval, limit, include_seed=include_seed)
    if include_seed and not items:
        loaded = await runtime.market_data.load_from_trades(
            session,
            market.id,
            market.symbol,
            price_precision=market.price_precision,
            qty_precision=market.qty_precision,
            include_seed=True,
        )
        if not loaded:
            await runtime.market_data.load_persisted_klines(
                session,
                market.id,
                market.symbol,
                include_seed=True,
            )
        items = runtime.market_data.get_public_klines(symbol, interval, limit, include_seed=include_seed)
    hidden_seed_items: list[dict] = []
    if not include_seed and not items:
        hidden_seed_items = [
            item
            for item in runtime.market_data.get_public_klines(symbol, interval, limit, include_seed=True)
            if _kline_item_has_seed(item)
        ]
    return {
        "items": items,
        "persistence": public_persistence_contract(run_id=getattr(runtime, "run_id", None)),
        "meta": _kline_meta(
            symbol=symbol,
            interval=interval,
            limit=limit,
            include_seed=include_seed,
            items=items,
            hidden_seed_items=hidden_seed_items,
            maker_instance=maker_instance,
            persistence=public_persistence_contract(run_id=getattr(runtime, "run_id", None)),
        ),
    }


@router.get("/markets/{symbol}/maker-instance")
async def get_public_maker_instance(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
    return _public_maker_status(market, request, instance)


@router.get("/markets/{symbol}/stats")
async def get_stats(symbol: str, request: Request):
    symbol = symbol.upper()
    runtime = get_runtime(request)
    orderbook, _, _ = await runtime.orderbook_snapshot(symbol)
    return runtime.market_data.compute_stats(symbol, orderbook)


@router.get("/markets/{symbol}/health")
async def get_market_health(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return await build_market_health(market, request, session)
