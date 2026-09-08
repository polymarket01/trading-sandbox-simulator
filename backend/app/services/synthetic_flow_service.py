from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    CONTRACT_TRADING_MODE_PAUSED,
    CONTRACT_TRADING_MODE_REDUCE_ONLY,
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_FILLED,
    ORDER_TYPE_LIMIT,
    POSITION_ACTION_OPEN,
    PRODUCT_TYPE_PERP,
    ROLE_BOT,
)
from app.core.decimal_utils import decimal_to_str, is_step_aligned, quantize_scale
from app.core.time_utils import to_millis
from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from app.models.user import User
from app.services.ids import next_order_id, next_trade_id
from app.services.sequencer import PreviewBboOrderCommand


FLOW_ORDER_PREFIXES = ("flowv2-", "flow-", "flowioc-", "perpflow-")
SYNTHETIC_FLOW_PERSISTENCE = "ephemeral_tape"
SYNTHETIC_FLOW_EXECUTION_MODE = "synthetic_ephemeral"


class SyntheticFlowValidationError(ValueError):
    pass


class SyntheticFlowUnavailableError(RuntimeError):
    """The writer became unsafe after synthetic admission began."""

    pass


def is_flow_client_order_id(value: object) -> bool:
    client_order_id = str(value or "")
    return bool(client_order_id) and (
        client_order_id.startswith(FLOW_ORDER_PREFIXES)
        or "-flow-" in client_order_id
    )


def synthetic_flow_candidate(user: Any, payload: Any) -> bool:
    return (
        settings.persistence_mode == "memory"
        and not settings.robot_flow_persistence_enabled
        and str(getattr(user, "role", "")) == ROLE_BOT
        and str(getattr(payload, "tif", "")) == "ioc"
        and is_flow_client_order_id(getattr(payload, "client_order_id", None))
    )


async def _current_authorized_market_bot_ids(
    session_factory: Any,
    *,
    market_id: int,
    user_ids: set[int],
) -> set[int]:
    """Read authority in a fresh transaction, independent of request-session state."""
    if not user_ids:
        return set()
    async with session_factory() as authority_session:
        rows = await authority_session.execute(
            select(User.id)
            .join(MarketBotAccount, MarketBotAccount.user_id == User.id)
            .where(
                User.id.in_(user_ids),
                User.is_active.is_(True),
                User.role == ROLE_BOT,
                MarketBotAccount.market_id == int(market_id),
                MarketBotAccount.is_enabled.is_(True),
                MarketBotAccount.role.in_(("flow", "maker")),
            )
            .distinct()
        )
        return {int(value) for value in rows.scalars()}


def validate_synthetic_flow_request(
    *,
    market: Market,
    user: Any,
    payload: Any,
    robot_user_ids: set[int],
) -> tuple[Decimal, Decimal] | None:
    """Validate fields without touching settings, balances or positions."""
    if not synthetic_flow_candidate(user, payload):
        return None
    try:
        taker_user_id = int(getattr(user, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if taker_user_id <= 0 or taker_user_id not in robot_user_ids:
        return None
    if str(getattr(payload, "type", "")) != ORDER_TYPE_LIMIT:
        raise SyntheticFlowValidationError("synthetic FLOW requires a limit IOC")
    if not market.is_active:
        raise SyntheticFlowValidationError("market is inactive")
    if market.product_type == PRODUCT_TYPE_PERP:
        trading_mode = str(market.contract_trading_mode or "normal")
        if trading_mode == CONTRACT_TRADING_MODE_PAUSED:
            raise SyntheticFlowValidationError("contract market is paused")
        if (
            trading_mode == CONTRACT_TRADING_MODE_REDUCE_ONLY
            and str(getattr(payload, "position_action", "")) == POSITION_ACTION_OPEN
        ):
            raise SyntheticFlowValidationError("contract market is reduce-only")

    raw_quantity = Decimal(str(getattr(payload, "quantity", "0")))
    raw_price = Decimal(str(getattr(payload, "price", "0")))
    quantity = quantize_scale(raw_quantity, market.qty_precision)
    limit_price = quantize_scale(raw_price, market.price_precision)
    qty_step = quantize_scale(market.qty_step, market.qty_precision)
    price_tick = quantize_scale(market.price_tick, market.price_precision)
    if raw_quantity != quantity or quantity <= 0 or not is_step_aligned(quantity, qty_step):
        raise SyntheticFlowValidationError("quantity does not match qty_step")
    if quantity < Decimal(market.min_qty):
        raise SyntheticFlowValidationError("quantity below min_qty")
    if raw_price != limit_price or limit_price <= 0 or not is_step_aligned(limit_price, price_tick):
        raise SyntheticFlowValidationError("price does not match tick")
    if limit_price * quantity < Decimal(market.min_notional):
        raise SyntheticFlowValidationError("notional below min_notional")
    return quantity, limit_price


def build_synthetic_flow_execution(
    *,
    market: Market,
    payload: Any,
    quantity: Decimal,
    limit_price: Decimal,
    preview_result: Any,
    force_no_fill_reason: str | None = None,
) -> dict | None:
    if preview_result is None:
        return None
    accepted_fills = [] if force_no_fill_reason else preview_result.fills
    filled_quantity = sum((Decimal(fill.quantity) for fill in accepted_fills), Decimal("0"))
    execution_notional = sum(
        (Decimal(fill.price) * Decimal(fill.quantity) for fill in accepted_fills), Decimal("0")
    )
    average_price = execution_notional / filled_quantity if filled_quantity > 0 else None
    now = datetime.now(tz=UTC)
    now_ms = to_millis(now)
    remaining_quantity = quantity - filled_quantity
    quote_amount = quantize_scale(execution_notional, 8)
    common_flags = {
        "synthetic": True,
        "execution_mode": SYNTHETIC_FLOW_EXECUTION_MODE,
        "durable": False,
        "financial_effect": False,
        "persistence": SYNTHETIC_FLOW_PERSISTENCE,
    }
    trades: list[dict] = []
    display_trade = None
    if filled_quantity > 0 and average_price is not None:
        trade_id = next_trade_id()
        trades.append(
            {
                "trade_id": trade_id,
                "symbol": market.symbol,
                "product_type": market.product_type,
                "price": decimal_to_str(average_price),
                "quantity": decimal_to_str(filled_quantity),
                "quote_amount": decimal_to_str(quote_amount),
                "taker_side": str(payload.side),
                "taker_position_action": getattr(payload, "position_action", None),
                "maker_position_action": None,
                "taker_realized_pnl": "0",
                "maker_realized_pnl": "0",
                "maker_fee": "0",
                "taker_fee": "0",
                "maker_fee_asset": market.margin_asset or market.quote_asset,
                "taker_fee_asset": market.margin_asset or market.quote_asset,
                "source": "synthetic_flow",
                "executed_at": now_ms,
                **common_flags,
            }
        )
        display_trade = {
            "trade_id": trade_id,
            "price": decimal_to_str(average_price),
            "quantity": decimal_to_str(filled_quantity),
            "side": str(payload.side),
            "ts": now_ms,
        }
    order = {
        "order_id": next_order_id(),
        "client_order_id": str(payload.client_order_id),
        "symbol": market.symbol,
        "product_type": market.product_type,
        "side": str(payload.side),
        "position_action": getattr(payload, "position_action", None),
        "reduce_only": bool(getattr(payload, "reduce_only", False)),
        "leverage": None,
        "type": ORDER_TYPE_LIMIT,
        "tif": "ioc",
        "status": ORDER_STATUS_FILLED if remaining_quantity <= 0 else ORDER_STATUS_CANCELED,
        "sequence_number": 0,
        "version": 0,
        "price": decimal_to_str(limit_price),
        "quantity": decimal_to_str(quantity),
        "filled_quantity": decimal_to_str(filled_quantity),
        "remaining_quantity": decimal_to_str(remaining_quantity),
        "avg_price": decimal_to_str(average_price) if average_price is not None else None,
        "notional": decimal_to_str(quote_amount),
        "reference_price": decimal_to_str(average_price) if average_price is not None else None,
        "protection_bps": None,
        "max_price": None,
        "min_price": None,
        "reject_reason": force_no_fill_reason,
        "created_at": now_ms,
        "updated_at": now_ms,
        **common_flags,
    }
    return {
        "order": order,
        "trades": trades,
        "fast_path": True,
        "skip_reason": force_no_fill_reason,
        **common_flags,
        "_display_trade": display_trade,
    }


async def execute_synthetic_flow(
    *,
    session: AsyncSession,
    runtime: Any,
    market: Market,
    user: User,
    payload: Any,
    fast_orders: dict[str, dict],
) -> dict | None:
    """Execute trusted FLOW without touching customers or financial state.

    ``None`` means the request is not an authoritative FLOW candidate and may
    use the normal durable route.  Once admitted, encountering any customer or
    unverifiable maker produces an all-or-nothing synthetic no-fill response;
    it must never fall through to real matching.
    """
    if not synthetic_flow_candidate(user, payload):
        return None
    writer = getattr(runtime, "persistence_writer", None)
    if writer is None or not writer.synthetic_flow_ready():
        raise SyntheticFlowUnavailableError(
            "synthetic FLOW unavailable: persistence writer is not healthy"
        )

    async with runtime.market_locks[market.symbol.upper()]:
        if not writer.synthetic_flow_ready():
            raise SyntheticFlowUnavailableError(
                "synthetic FLOW unavailable: persistence writer is not healthy"
            )
        # Re-read current authority while holding the same market lock used by
        # the FIFO preview.  A stale ORM object or a client-id convention can
        # never grant the display-only route.
        taker_ids = await _current_authorized_market_bot_ids(
            writer._session_factory,
            market_id=int(market.id),
            user_ids={int(user.id)},
        )
        if taker_ids != {int(user.id)}:
            return None
        validated = validate_synthetic_flow_request(
            market=market,
            user=user,
            payload=payload,
            robot_user_ids=writer.robot_user_ids_snapshot(),
        )
        if validated is None:
            return None
        quantity, limit_price = validated
        cache_key = (int(user.id), int(market.id), str(payload.client_order_id))
        request_fingerprint = (
            str(payload.side),
            ORDER_TYPE_LIMIT,
            "ioc",
            decimal_to_str(quantity),
            decimal_to_str(limit_price),
            str(getattr(payload, "position_action", None) or ""),
            bool(getattr(payload, "reduce_only", False)),
        )
        cache = getattr(runtime, "synthetic_flow_idempotency", None)
        if cache is None:
            cache = runtime.synthetic_flow_idempotency = {}
        cached = cache.get(cache_key)
        if cached is not None:
            if (
                not isinstance(cached, dict)
                or cached.get("request_fingerprint") != request_fingerprint
                or not isinstance(cached.get("response"), dict)
            ):
                raise SyntheticFlowValidationError(
                    "client_order_id conflicts with a different synthetic FLOW request"
                )
            response = deepcopy(cached["response"])
            response["idempotent"] = True
            response["_synthetic_new"] = False
            return response

        robot_user_ids = writer.robot_user_ids_snapshot()

        def maker_guard(
            order_id: str,
            maker_user_id: int,
            price: Decimal,
            remaining: Decimal,
        ) -> bool:
            snap = fast_orders.get(str(order_id))
            if snap is None or int(maker_user_id) not in robot_user_ids:
                return False
            try:
                return (
                    int(snap.get("user_id") or 0) == int(maker_user_id)
                    and str(snap.get("order_id") or "") == str(order_id)
                    and Decimal(str(snap.get("price") or "0")) == Decimal(price)
                    and Decimal(str(snap.get("remaining_quantity") or "0"))
                    == Decimal(remaining)
                    and str(snap.get("status") or "")
                    in {"new", "partially_filled"}
                    and str(snap.get("type") or "") == "limit"
                    and str(snap.get("tif") or "") == "gtc"
                )
            except (TypeError, ValueError):
                return False

        preview = await runtime.get_symbol_sequencer(market.symbol).submit(
            PreviewBboOrderCommand(
                side=str(payload.side),
                quantity=quantity,
                limit_price=limit_price,
                maker_guard=maker_guard,
            )
        )
        preview_result = preview.engine_result
        if preview_result is None:
            return None
        force_no_fill_reason = None
        if preview_result.stop_reason:
            force_no_fill_reason = "flow_avoids_non_robot_liquidity"
        else:
            maker_user_ids = {int(fill.maker_user_id) for fill in preview_result.fills}
            authorized_makers = await _current_authorized_market_bot_ids(
                writer._session_factory,
                market_id=int(market.id),
                user_ids=maker_user_ids,
            )
            if authorized_makers != maker_user_ids:
                force_no_fill_reason = "flow_avoids_non_robot_liquidity"
        # No await is allowed between this final health gate and display-tape
        # ingestion, so a HALTED transition cannot leak a synthetic print.
        if not writer.synthetic_flow_ready():
            raise SyntheticFlowUnavailableError(
                "synthetic FLOW aborted: persistence writer became unhealthy"
            )
        response = build_synthetic_flow_execution(
            market=market,
            payload=payload,
            quantity=quantity,
            limit_price=limit_price,
            preview_result=preview_result,
            force_no_fill_reason=force_no_fill_reason,
        )
        if response is None:
            return None
        display = response.pop("_display_trade")
        if display is not None:
            runtime.market_data.ingest_display_trade(
                market.symbol,
                price=Decimal(display["price"]),
                quantity=Decimal(display["quantity"]),
                side=str(display["side"]),
                ts=datetime.fromtimestamp(int(display["ts"]) / 1000, tz=UTC),
                trade_id=str(display["trade_id"]),
                price_scale=market.price_precision,
                qty_scale=market.qty_precision,
                source="synthetic_flow",
            )
        cache[cache_key] = {
            "request_fingerprint": request_fingerprint,
            "response": deepcopy(response),
        }
        while len(cache) > 10_000:
            cache.pop(next(iter(cache)))
        writer.record_synthetic_flow(market.symbol, len(response["trades"]))
        response["_synthetic_new"] = True
        return response


async def broadcast_synthetic_flow(runtime: Any, market: Market, trades: list[dict]) -> None:
    """Publish display tape and isolated public candles, never canonical K-lines."""
    if not trades:
        return
    await runtime.ws.broadcast_public(
        "trades",
        market.symbol,
        {
            "channel": "trades",
            "type": "update",
            "symbol": market.symbol,
            "items": trades,
        },
    )
    for interval in ("1s", "5s", "15s", "1m", "5m", "15m"):
        items = runtime.market_data.get_public_klines(market.symbol, interval, 1)
        if not items:
            continue
        await runtime.ws.broadcast_public(
            "kline",
            market.symbol,
            {
                "channel": "kline",
                "type": "update",
                "symbol": market.symbol,
                "interval": interval,
                "kline": items[-1],
            },
            interval=interval,
        )
