from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.decimal_utils import decimal_to_str
from app.core.time_utils import to_millis
from app.models.market import Market


FLOW_MODE_OFF = "off"
FLOW_MODE_REAL_IOC_SANDBOX = "real_ioc_sandbox"
FLOW_MODE_VIRTUAL_VOLUME = "virtual_volume"
LOCAL_SANDBOX_CONNECTORS = {"local", "local_sandbox", "sandbox", "builtin"}


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _flow_control(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("flow_control")
    return value if isinstance(value, dict) else {}


def resolve_flow_mode(config: dict[str, Any]) -> str:
    control = _flow_control(config)
    if control:
        if not _bool(control.get("enabled"), default=False):
            return FLOW_MODE_OFF
        mode = str(control.get("mode") or FLOW_MODE_REAL_IOC_SANDBOX).strip().lower()
    else:
        mode = str(config.get("flow_mode") or "").strip().lower()
        if not mode:
            mode = FLOW_MODE_REAL_IOC_SANDBOX if _bool(config.get("flow_enabled"), default=False) else FLOW_MODE_OFF
        if mode == "real_ioc":
            mode = FLOW_MODE_REAL_IOC_SANDBOX
    if mode not in {FLOW_MODE_OFF, FLOW_MODE_REAL_IOC_SANDBOX, FLOW_MODE_VIRTUAL_VOLUME}:
        return FLOW_MODE_OFF
    return mode


def flow_connector(config: dict[str, Any]) -> str:
    control = _flow_control(config)
    return str(
        control.get("connector")
        or config.get("flow_connector")
        or config.get("exchange_connector")
        or "local_sandbox"
    ).strip().lower()


def flow_limits(config: dict[str, Any]) -> dict[str, Any]:
    control = _flow_control(config)
    source = {**config, **control}
    return {
        "max_deviation_bps": _decimal(source.get("max_deviation_bps"), Decimal("5")),
        "allowed_layers": max(1, _int(source.get("allowed_layers"), 1)),
        "max_in_flight": max(1, _int(source.get("flow_max_in_flight"), 1)),
        "queue_pause_size": max(1, _int(source.get("flow_pause_queue_size"), 24)),
        "turnover_quote_per_min": _decimal(source.get("flow_turnover_quote_per_min"), Decimal("600000")),
        "clip_quote": _decimal(source.get("flow_clip_quote") or source.get("idle_clip_usdt"), Decimal("6000")),
        "max_level_take_ratio": _decimal(source.get("flow_max_level_take_ratio"), Decimal("0.30")),
        "min_level_remaining_usdt": _decimal(source.get("flow_min_level_remaining_usdt"), Decimal("1500")),
        "max_inventory_quote": _decimal(source.get("max_inventory_quote"), Decimal("0")),
        "max_loss_quote": _decimal(source.get("max_loss_quote"), Decimal("0")),
        "max_cancel_replace_per_min": _int(source.get("max_cancel_replace_per_min"), 120),
        "max_queue_backlog": max(1, _int(source.get("max_queue_backlog") or source.get("flow_pause_queue_size"), 24)),
        "min_book_levels_per_side": max(0, _int(source.get("flow_min_book_levels_per_side"), 0)),
    }


def _extract_levels(orderbook: dict[str, Any], side: str) -> list[tuple[Decimal, Decimal]]:
    raw_levels = orderbook.get(side) if isinstance(orderbook, dict) else None
    if not isinstance(raw_levels, list):
        return []
    levels: list[tuple[Decimal, Decimal]] = []
    for item in raw_levels:
        if isinstance(item, dict):
            price = _decimal(item.get("price"))
            qty = _decimal(item.get("quantity") or item.get("qty") or item.get("amount"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            price = _decimal(item[0])
            qty = _decimal(item[1])
        else:
            continue
        if price > 0 and qty > 0:
            levels.append((price, qty))
    return levels


def build_orderbook_context(orderbook: dict[str, Any] | None, metrics: dict[str, Any] | None = None) -> dict[str, Any]:
    metrics = metrics or {}
    orderbook = orderbook or {}
    bids = _extract_levels(orderbook, "bids")
    asks = _extract_levels(orderbook, "asks")
    best_bid = bids[0][0] if bids else _decimal(metrics.get("best_bid"))
    best_ask = asks[0][0] if asks else _decimal(metrics.get("best_ask"))
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bids": bids,
        "asks": asks,
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }


def resolve_fair_price(
    market: Market,
    *,
    metrics: dict[str, Any] | None = None,
    orderbook: dict[str, Any] | None = None,
    contract_price_snapshot: dict[str, Any] | None = None,
) -> tuple[Decimal, str]:
    metrics = metrics or {}
    snapshot = contract_price_snapshot or {}
    candidates = [
        ("fair_price", metrics.get("fair_price")),
        ("index_price", snapshot.get("index_price") or metrics.get("index_price")),
        ("mark_price", snapshot.get("mark_price") or metrics.get("mark_price")),
        ("external_mid", metrics.get("external_mid")),
        ("mid_price", metrics.get("mid_price")),
        ("reference_price", market.reference_price),
    ]
    book = build_orderbook_context(orderbook, metrics)
    if book["best_bid"] > 0 and book["best_ask"] > 0:
        candidates.insert(4, ("local_bbo_mid", (book["best_bid"] + book["best_ask"]) / Decimal("2")))
    for source, value in candidates:
        price = _decimal(value)
        if price > 0:
            return price, source
    return Decimal("0"), "missing"


def _deviation_bps(price: Decimal, fair_price: Decimal) -> Decimal:
    if fair_price <= 0:
        return Decimal("999999")
    return abs(price - fair_price) / fair_price * Decimal("10000")


def evaluate_flow_price(
    *,
    side: str,
    price: Decimal | str | int | float,
    fair_price: Decimal | str | int | float,
    bids: list[tuple[Decimal, Decimal]] | list[Decimal],
    asks: list[tuple[Decimal, Decimal]] | list[Decimal],
    max_deviation_bps: Decimal | str | int | float,
    allowed_layers: int,
) -> dict[str, Any]:
    normalized_price = _decimal(price)
    normalized_fair = _decimal(fair_price)
    normalized_deviation = _decimal(max_deviation_bps, Decimal("5"))
    bid_prices = [item[0] if isinstance(item, tuple) else _decimal(item) for item in bids]
    ask_prices = [item[0] if isinstance(item, tuple) else _decimal(item) for item in asks]
    bid_prices = [item for item in bid_prices if item > 0]
    ask_prices = [item for item in ask_prices if item > 0]
    layers = max(1, int(allowed_layers or 1))

    if normalized_price <= 0 or normalized_fair <= 0:
        return {"allowed": False, "reason": "missing_price", "deviation_bps": None}
    deviation = _deviation_bps(normalized_price, normalized_fair)
    if deviation > normalized_deviation:
        return {
            "allowed": False,
            "reason": "fair_price_deviation",
            "deviation_bps": decimal_to_str(deviation),
            "max_deviation_bps": decimal_to_str(normalized_deviation),
        }
    if not bid_prices or not ask_prices:
        return {"allowed": False, "reason": "missing_bbo", "deviation_bps": decimal_to_str(deviation)}

    best_bid = bid_prices[0]
    best_ask = ask_prices[0]
    if best_bid <= normalized_price <= best_ask:
        return {"allowed": True, "reason": "inside_bbo", "deviation_bps": decimal_to_str(deviation)}

    if side == "buy":
        allowed_index = min(layers, len(ask_prices)) - 1
        allowed_price = ask_prices[allowed_index]
        if normalized_price <= allowed_price:
            return {
                "allowed": True,
                "reason": "within_allowed_ask_layers",
                "deviation_bps": decimal_to_str(deviation),
                "allowed_price": decimal_to_str(allowed_price),
                "allowed_layers": layers,
            }
        return {
            "allowed": False,
            "reason": "crosses_too_many_ask_layers",
            "deviation_bps": decimal_to_str(deviation),
            "allowed_price": decimal_to_str(allowed_price),
            "allowed_layers": layers,
        }

    allowed_index = min(layers, len(bid_prices)) - 1
    allowed_price = bid_prices[allowed_index]
    if normalized_price >= allowed_price:
        return {
            "allowed": True,
            "reason": "within_allowed_bid_layers",
            "deviation_bps": decimal_to_str(deviation),
            "allowed_price": decimal_to_str(allowed_price),
            "allowed_layers": layers,
        }
    return {
        "allowed": False,
        "reason": "crosses_too_many_bid_layers",
        "deviation_bps": decimal_to_str(deviation),
        "allowed_price": decimal_to_str(allowed_price),
        "allowed_layers": layers,
    }


def _source_distribution(source_counts: dict[str, Any] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, value in (source_counts or {}).items():
        count = _int(value)
        if count > 0:
            result[str(key)] = count
    return result


def _decimal_str_or_none(value: Any) -> str | None:
    numeric = _decimal(value)
    return decimal_to_str(numeric) if numeric > 0 else None


def build_symbol_coordinator_state(
    *,
    market: Market,
    strategy_config: dict[str, Any] | None,
    liquidity_metrics: dict[str, Any] | None,
    orderbook: dict[str, Any] | None,
    instance_status: dict[str, Any] | None,
    bots: list[dict[str, Any]] | None = None,
    contract_price_snapshot: dict[str, Any] | None = None,
    source_counts: dict[str, Any] | None = None,
    persistence_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = strategy_config or {}
    metrics = liquidity_metrics or {}
    instance = instance_status or {}
    bots = bots or []
    book = build_orderbook_context(orderbook, metrics)
    fair_price, fair_source = resolve_fair_price(
        market,
        metrics=metrics,
        orderbook=orderbook,
        contract_price_snapshot=contract_price_snapshot,
    )
    limits = flow_limits(config)
    mode = resolve_flow_mode(config)
    connector = flow_connector(config)
    mm_running = bool(instance.get("running")) and str(instance.get("status") or "") not in {"stale", "error", "stopped"}
    flow_ioc = metrics.get("flow_ioc") if isinstance(metrics.get("flow_ioc"), dict) else {}
    persistence = persistence_metrics or {}
    persistence_policy = str(persistence.get("robot_flow_effective_policy") or "")
    runner_execution_mode = str(flow_ioc.get("effective_execution_mode") or "")
    if mode == FLOW_MODE_VIRTUAL_VOLUME:
        effective_execution_mode = "virtual_volume"
    elif mode == FLOW_MODE_REAL_IOC_SANDBOX:
        effective_execution_mode = (
            "synthetic_ephemeral"
            if persistence_policy == "synthetic_ephemeral"
            or (not persistence_policy and runner_execution_mode == "synthetic_ephemeral")
            else "durable"
        )
    else:
        effective_execution_mode = "disabled"
    synthetic_execution = effective_execution_mode == "synthetic_ephemeral"
    financial_filled = _int(
        flow_ioc.get("financial_filled")
        if "financial_filled" in flow_ioc
        else flow_ioc.get("filled")
    )
    queue_size = _int(metrics.get("action_queue_size") or metrics.get("q") or flow_ioc.get("queue_size"))
    in_flight = _int(flow_ioc.get("in_flight"))
    pause_reason = ""
    flow_allowed = mode != FLOW_MODE_OFF
    if not flow_allowed:
        pause_reason = "flow_disabled"
    elif connector not in LOCAL_SANDBOX_CONNECTORS:
        flow_allowed = False
        pause_reason = "external_connector_disabled"
    elif not mm_running:
        flow_allowed = False
        pause_reason = "mm_stopped"
    elif fair_price <= 0 or book["best_bid"] <= 0 or book["best_ask"] <= 0:
        flow_allowed = False
        pause_reason = "missing_bbo_or_fair_price"
    elif queue_size >= limits["queue_pause_size"]:
        flow_allowed = False
        pause_reason = "queue_backlog"
    elif in_flight >= limits["max_in_flight"]:
        flow_allowed = False
        pause_reason = "in_flight_limit"
    elif limits["min_book_levels_per_side"] > 0 and (
        book["bid_levels"] < limits["min_book_levels_per_side"] or book["ask_levels"] < limits["min_book_levels_per_side"]
    ):
        flow_allowed = False
        pause_reason = "thin_book"
    metrics_flow_allowed = metrics.get("flow_allowed")
    metrics_pause_reason = (
        metrics.get("flow_pause_reason")
        or metrics.get("flow_guard_reason")
        or flow_ioc.get("pause_reason")
        or flow_ioc.get("guard_reason")
        or flow_ioc.get("last_status")
    )
    if flow_allowed and metrics_flow_allowed is False:
        flow_allowed = False
        pause_reason = str(metrics_pause_reason or "flow_runtime_paused")

    buy_probe = evaluate_flow_price(
        side="buy",
        price=book["best_ask"],
        fair_price=fair_price,
        bids=book["bids"],
        asks=book["asks"],
        max_deviation_bps=limits["max_deviation_bps"],
        allowed_layers=limits["allowed_layers"],
    )
    sell_probe = evaluate_flow_price(
        side="sell",
        price=book["best_bid"],
        fair_price=fair_price,
        bids=book["bids"],
        asks=book["asks"],
        max_deviation_bps=limits["max_deviation_bps"],
        allowed_layers=limits["allowed_layers"],
    )
    if flow_allowed and (not buy_probe.get("allowed") or not sell_probe.get("allowed")):
        flow_allowed = False
        pause_reason = "price_band_blocked"

    makers = [item for item in bots if item.get("role") == "maker"]
    flows = [item for item in bots if item.get("role") == "flow"]
    flow_status = "stopped" if mode == FLOW_MODE_OFF else "running" if flow_allowed else "paused"
    return {
        "symbol": market.symbol,
        "product_type": market.product_type,
        "fair_price": decimal_to_str(fair_price) if fair_price > 0 else None,
        "fair_price_source": fair_source,
        "index_price": _decimal_str_or_none((contract_price_snapshot or {}).get("index_price")),
        "mark_price": _decimal_str_or_none((contract_price_snapshot or {}).get("mark_price")),
        "bbo": {
            "best_bid": decimal_to_str(book["best_bid"]) if book["best_bid"] > 0 else None,
            "best_ask": decimal_to_str(book["best_ask"]) if book["best_ask"] > 0 else None,
            "bid_levels": book["bid_levels"],
            "ask_levels": book["ask_levels"],
        },
        "mm": {
            "running": mm_running,
            "status": instance.get("status") or "stopped",
            "enabled_makers": len([item for item in makers if item.get("is_enabled", True)]),
        },
        "flow": {
            "status": flow_status,
            "running": flow_status == "running",
            "mode": mode,
            "connector": connector,
            "allowed": flow_allowed,
            "flow_allowed": flow_allowed,
            "pause_reason": pause_reason or None,
            "enabled_flows": len([item for item in flows if item.get("is_enabled", True)]),
            "in_flight": in_flight,
            "success_rate": flow_ioc.get("success_rate"),
            "display_success_rate": flow_ioc.get("display_success_rate") or flow_ioc.get("success_rate"),
            "financial_success_rate": flow_ioc.get("financial_success_rate"),
            "attempts": _int(flow_ioc.get("attempts")),
            "filled": _int(flow_ioc.get("filled")),
            "financial_filled": financial_filled,
            "synthetic": _int(flow_ioc.get("synthetic")),
            "synthetic_no_fill": _int(flow_ioc.get("synthetic_no_fill")),
            "failed": _int(flow_ioc.get("failed")),
            "skipped_queue": _int(flow_ioc.get("skipped_queue")),
            "queue_size": queue_size,
            "queue_limit": limits["queue_pause_size"],
            "buy_price_check": buy_probe,
            "sell_price_check": sell_probe,
            "effective_execution_mode": effective_execution_mode,
            "financial_effect": False if synthetic_execution or mode == FLOW_MODE_VIRTUAL_VOLUME else mode == FLOW_MODE_REAL_IOC_SANDBOX,
            "source_policy": (
                "synthetic_flow_display_only"
                if synthetic_execution
                else "real_ioc_sandbox_enters_matching"
                if mode == FLOW_MODE_REAL_IOC_SANDBOX
                else "virtual_volume_not_matching"
                if mode == FLOW_MODE_VIRTUAL_VOLUME
                else "disabled"
            ),
        },
        "limits": {
            "max_deviation_bps": decimal_to_str(limits["max_deviation_bps"]),
            "allowed_layers": limits["allowed_layers"],
            "max_in_flight": limits["max_in_flight"],
            "turnover_quote_per_min": decimal_to_str(limits["turnover_quote_per_min"]),
            "clip_quote": decimal_to_str(limits["clip_quote"]),
            "max_level_take_ratio": decimal_to_str(limits["max_level_take_ratio"]),
            "min_level_remaining_usdt": decimal_to_str(limits["min_level_remaining_usdt"]),
            "max_inventory_quote": decimal_to_str(limits["max_inventory_quote"]),
            "max_loss_quote": decimal_to_str(limits["max_loss_quote"]),
            "max_cancel_replace_per_min": limits["max_cancel_replace_per_min"],
            "max_queue_backlog": limits["max_queue_backlog"],
            "min_book_levels_per_side": limits["min_book_levels_per_side"],
        },
        "source_distribution": _source_distribution(source_counts),
        "warnings": [
            warning
            for warning in (
                "真实外部交易所 connector 默认禁用主动自成交/刷量。" if connector not in LOCAL_SANDBOX_CONNECTORS else "",
                "virtual_volume 进入内存 T&S 与展示 K 线，不进入撮合或账本。" if mode == FLOW_MODE_VIRTUAL_VOLUME else "",
                "synthetic FLOW 只生成展示成交和隔离的展示 K 线，不进入撮合、不改资金/仓位/账本，不写 canonical K 线。" if synthetic_execution else "",
            )
            if warning
        ],
    }


def build_symbol_robot_cards(
    *,
    market: Market,
    instance_status: dict[str, Any],
    bots: list[dict[str, Any]],
    coordinator: dict[str, Any],
    log_lines: list[str] | None = None,
) -> list[dict[str, Any]]:
    makers = [item for item in bots if item.get("role") == "maker"]
    flows = [item for item in bots if item.get("role") == "flow"]
    mm_running = bool(instance_status.get("running"))
    flow_state = coordinator.get("flow", {}) if isinstance(coordinator.get("flow"), dict) else {}
    mode = flow_state.get("mode") or FLOW_MODE_OFF
    flow_status = str(flow_state.get("status") or ("running" if mode != FLOW_MODE_OFF else "stopped"))
    return [
        {
            "id": f"{market.symbol}_MM_1",
            "symbol": market.symbol,
            "role": "MM",
            "kind": "market_maker",
            "status": instance_status.get("status") or ("running" if mm_running else "stopped"),
            "running": mm_running,
            "uid": makers[0].get("uid") if makers else None,
            "account": makers[0].get("username") if makers else None,
            "enabled_accounts": len([item for item in makers if item.get("is_enabled", True)]),
            "open_order_count": sum(_int(item.get("open_order_count")) for item in makers),
            "controls": {
                "start": f"/admin/markets/{market.symbol}/maker-instance/start",
                "pause": f"/admin/markets/{market.symbol}/maker-instance/stop",
                "restart": f"/admin/markets/{market.symbol}/maker-instance/restart",
                "logs": f"/admin/markets/{market.symbol}/maker-instance/logs",
            },
            "metrics": {
                "fair_price": coordinator.get("fair_price"),
                "bbo": coordinator.get("bbo"),
            },
            "logs": log_lines or [],
        },
        {
            "id": f"{market.symbol}_FLOW_1",
            "symbol": market.symbol,
            "role": "FLOW",
            "kind": "active_flow",
            "status": flow_status,
            "running": flow_status == "running",
            "uid": flows[0].get("uid") if flows else None,
            "account": flows[0].get("username") if flows else None,
            "enabled_accounts": len([item for item in flows if item.get("is_enabled", True)]),
            "open_order_count": sum(_int(item.get("open_order_count")) for item in flows),
            "controls": {
                "start": f"/admin/markets/{market.symbol}/flow/start",
                "pause": f"/admin/markets/{market.symbol}/flow/pause",
                "config": f"/admin/markets/{market.symbol}/strategy",
                "logs": f"/admin/markets/{market.symbol}/maker-instance/logs",
            },
            "metrics": deepcopy(flow_state),
            "logs": [],
        },
    ]


def record_virtual_volume(
    runtime: Any,
    *,
    symbol: str,
    side: str,
    price: Decimal | str | int | float,
    quantity: Decimal | str | int | float,
    ts_ms: int | None = None,
) -> dict[str, Any]:
    if not hasattr(runtime, "virtual_volume_metrics"):
        runtime.virtual_volume_metrics = {}
    key = symbol.upper()
    now_ms = ts_ms or to_millis(datetime.now(tz=UTC))
    normalized_price = _decimal(price)
    normalized_qty = _decimal(quantity)
    quote_amount = normalized_price * normalized_qty
    current = runtime.virtual_volume_metrics.setdefault(
        key,
        {
            "symbol": key,
            "source": FLOW_MODE_VIRTUAL_VOLUME,
            "trade_count": 0,
            "quote_volume": "0",
            "base_volume": "0",
            "last_event": None,
        },
    )
    current["trade_count"] = _int(current.get("trade_count")) + 1
    current["quote_volume"] = decimal_to_str(_decimal(current.get("quote_volume")) + quote_amount)
    current["base_volume"] = decimal_to_str(_decimal(current.get("base_volume")) + normalized_qty)
    current["last_event"] = {
        "symbol": key,
        "side": side,
        "price": decimal_to_str(normalized_price),
        "quantity": decimal_to_str(normalized_qty),
        "quote_amount": decimal_to_str(quote_amount),
        "source": FLOW_MODE_VIRTUAL_VOLUME,
        "ts": now_ms,
        "note": "virtual_volume_not_matching",
    }
    return deepcopy(current)
