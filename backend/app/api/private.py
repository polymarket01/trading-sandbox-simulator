from __future__ import annotations

from app.services.matching_faults import NotExecuted
from app.api.causal_helpers import matching_ack_response

import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.time_utils import to_millis
from app.db.session import get_db_session
from app.models.fee_profile import FeeProfile
from app.models.market import Market
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import (
    CancelAllRequest,
    CancelBatchRequest,
    OrderAmendRequest,
    OrderBatchAmendRequest,
    OrderBatchCreateRequest,
    OrderCreateRequest,
)
from app.schemas.quote_set import QuoteSetReplaceRequest
from app.services.order_service import OrderService, OrderValidationError
from app.services.quote_set_service import QuoteSetService
from app.services.rate_limiter import RateLimitExceeded
from app.services.synthetic_flow_service import SyntheticFlowUnavailableError
from app.services.persistence_contract import legacy_sampled_runtime
from app.core.constants import ROLE_BOT
from app.api.causal_helpers import (
    begin_causal_command,
    causal_command_context,
    complete_causal_command,
    existing_response_or_raise,
    mark_unknown_causal,
    reject_causal_command,
)

router = APIRouter(tags=["private"])


def get_runtime(request: Request):
    return request.app.state.runtime


def get_order_service(request: Request) -> OrderService:
    return request.app.state.order_service


def get_quote_set_service(request: Request) -> QuoteSetService:
    return request.app.state.quote_set_service


def build_external_base_url(request: Request) -> tuple[str, str]:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or request.url.netloc
    )
    base_url = f"{proto}://{host}".rstrip("/")
    ws_proto = "wss" if proto == "https" else "ws"
    return base_url, ws_proto


def private_order_limit_profile() -> dict:
    return {
        "enabled": settings.private_order_rate_limit_enabled,
        "order_submit": {
            "rate_per_second": settings.order_submit_rate_per_second,
            "burst": settings.order_submit_burst,
        },
        "order_submit_batch": {
            "rate_per_second": settings.order_submit_batch_rate_per_second,
            "burst": settings.order_submit_batch_burst,
            "max_orders": settings.order_submit_batch_max_orders,
        },
        "order_cancel": {
            "rate_per_second": settings.order_cancel_rate_per_second,
            "burst": settings.order_cancel_burst,
        },
        "order_amend": {
            "rate_per_second": settings.order_amend_rate_per_second,
            "burst": settings.order_amend_burst,
        },
        "order_cancel_all": {
            "rate_per_second": settings.order_cancel_all_rate_per_second,
            "burst": settings.order_cancel_all_burst,
        },
        "order_cancel_batch": {
            "rate_per_second": settings.order_cancel_all_rate_per_second,
            "burst": settings.order_cancel_all_burst,
        },
    }


def account_open_order_limit_profile() -> dict:
    return {
        "max_open_orders_per_user_market": settings.max_open_orders_per_user_market,
        "max_open_orders_per_user_total": settings.max_open_orders_per_user_total,
        "disabled_when_less_or_equal_zero": True,
    }


async def enforce_private_order_rate_limit(request: Request, user: User, action: str) -> None:
    if not settings.private_order_rate_limit_enabled:
        return
    if user.role == ROLE_BOT:
        # Demo maker/FLOW traffic is the liquidity generator; it uses the
        # in-memory fast path and is intentionally not throttled at demo rates.
        return
    profile = private_order_limit_profile().get(action)
    if not isinstance(profile, dict):
        return
    limiter = request.app.state.runtime.rate_limiter
    try:
        await limiter.check(
            f"user:{user.id}:private_order:{action}",
            action=action,
            rate_per_second=float(profile["rate_per_second"]),
            burst=int(profile["burst"]),
        )
    except RateLimitExceeded as exc:
        retry_after = max(1, math.ceil(exc.retry_after_seconds))
        raise HTTPException(
            status_code=429,
            detail=(
                f"rate limit exceeded for {exc.action}; "
                f"limit={exc.rate_per_second:g}/s burst={exc.burst}, retry_after={retry_after}s"
            ),
            headers={"Retry-After": str(retry_after)},
        ) from exc


@router.get("/account/balances")
async def get_balances(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    return {"items": await service.serialize_balances(session, user.id)}


@router.get("/account/fees")
async def get_account_fees(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    market_rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    markets = list(market_rows.scalars())
    fee_rows = await session.execute(select(FeeProfile).where(FeeProfile.user_id == user.id))
    fees = {item.market_id: item for item in fee_rows.scalars()}
    items = []
    for market in markets:
        profile = fees.get(market.id)
        items.append(
            {
                "symbol": market.symbol,
                "product_type": market.product_type,
                "market_type": market.market_type,
                "maker_fee_rate": str(profile.maker_fee_rate if profile else market.default_maker_fee_rate),
                "taker_fee_rate": str(profile.taker_fee_rate if profile else market.default_taker_fee_rate),
                "source": "user" if profile else "market_default",
            }
        )
    return {"items": items}


@router.get("/account/connectivity")
async def get_account_connectivity(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    base_url, ws_proto = build_external_base_url(request)
    market_stmt = select(Market).order_by(Market.symbol.asc())
    if symbol:
        market_stmt = market_stmt.where(Market.symbol == symbol.upper())
    market_rows = await session.execute(market_stmt)
    markets = list(market_rows.scalars())
    if symbol and not markets:
        raise HTTPException(status_code=404, detail="market not found")

    fee_rows = await session.execute(select(FeeProfile).where(FeeProfile.user_id == user.id))
    fees = {item.market_id: item for item in fee_rows.scalars()}
    return {
        "server_time": to_millis(datetime.now(tz=UTC)),
        "account": {
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "is_active": user.is_active,
            "api_key": user.api_key,
            "api_secret": user.api_secret_hash,
        },
        "endpoints": {
            "rest_base_url": f"{base_url}{settings.api_prefix}",
            "public_ws_url": f"{ws_proto}://{base_url.split('://', 1)[1]}/ws/public",
            "private_ws_url": f"{ws_proto}://{base_url.split('://', 1)[1]}/ws/private",
            "openapi_url": f"{base_url}/docs",
        },
        "auth": {
            "rest_header": "X-API-Key",
            "ws_signature_algorithm": "HMAC-SHA256",
            "ws_signature_payload": "{api_key}:{timestamp}",
            "ws_timestamp_ttl_ms": settings.default_ws_signature_ttl_ms,
        },
        "recommended_client_limits": {
            "note": "server-enforced for private order writes; tune down first on 4c6g before raising rates",
            **private_order_limit_profile(),
            "account_open_order_limits": account_open_order_limit_profile(),
            "public_orderbook_depth_max": 50,
            "history_limit_max": 500,
            "public_ws_channels": ["orderbook", "trades", "kline", "stats"],
            "private_ws_channels": ["balances", "orders", "trades", "ledger", "contracts"],
        },
        "markets": [
            {
                "symbol": market.symbol,
                "market_type": market.market_type,
                "base_asset": market.base_asset,
                "quote_asset": market.quote_asset,
                "margin_asset": market.margin_asset,
                "is_active": market.is_active,
                "max_leverage": str(market.max_leverage),
                "default_leverage": str(market.default_leverage),
                "maintenance_margin_rate": str(market.maintenance_margin_rate),
                "funding_rate": str(market.funding_rate),
                "funding_interval_hours": market.funding_interval_hours,
                "index_price_source": market.index_price_source,
                "mark_price_mode": market.mark_price_mode,
                "funding_rate_mode": market.funding_rate_mode,
                "funding_interest_rate": str(market.funding_interest_rate),
                "funding_clamp_rate": str(market.funding_clamp_rate),
                "funding_cap_rate": str(market.funding_cap_rate),
                "funding_impact_notional": str(market.funding_impact_notional),
                "price_tick": str(market.price_tick),
                "qty_step": str(market.qty_step),
                "min_qty": str(market.min_qty),
                "min_notional": str(market.min_notional),
                "price_precision": market.price_precision,
                "qty_precision": market.qty_precision,
                "maker_fee_rate": str(fees[market.id].maker_fee_rate if market.id in fees else market.default_maker_fee_rate),
                "taker_fee_rate": str(fees[market.id].taker_fee_rate if market.id in fees else market.default_taker_fee_rate),
                "fee_source": "user" if market.id in fees else "market_default",
            }
            for market in markets
        ],
    }


@router.get("/account/orders/open")
async def get_open_orders(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    if (settings.persistence_mode == "memory" and str(user.role) == ROLE_BOT):
        items = await service.fast_open_order_items(session, user.id, symbol=symbol)
        return {"items": items, "state_source": "fast_mirror"}
    stmt = select(Order, Market.symbol).join(Market, Market.id == Order.market_id).where(
        Order.user_id == user.id,
        Order.status.in_(["new", "partially_filled"]),
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol)
    rows = await session.execute(stmt.order_by(Order.created_at.desc()))
    items = [await service.serialize_order(session, order, market_symbol) for order, market_symbol in rows.all()]
    return {"items": items, "state_source": "database"}


@router.get("/account/orders/history")
async def get_order_history(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    stmt = select(Order, Market.symbol).join(Market, Market.id == Order.market_id).where(Order.user_id == user.id)
    if symbol:
        stmt = stmt.where(Market.symbol == symbol)
    rows = await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
    items = [await service.serialize_order(session, order, market_symbol) for order, market_symbol in rows.all()]
    return {"items": items}


@router.get("/account/trades")
async def get_account_trades(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    stmt = (
        select(Trade, Market)
        .join(Market, Market.id == Trade.market_id)
        .where(or_(Trade.taker_user_id == user.id, Trade.maker_user_id == user.id))
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol)
    rows = await session.execute(stmt.order_by(Trade.executed_at.desc()).limit(limit))
    items = [
        await service.serialize_account_trade(trade, market.symbol, user.id, market=market)
        for trade, market in rows.all()
    ]
    return {"items": items}


@router.get("/account/ledger")
async def get_ledger(
    request: Request,
    asset: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    return {"items": await service.serialize_ledger_entries(session, user.id, asset, limit)}


@router.post("/orders")
async def create_order(
    payload: OrderCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_submit")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_PLACE",
        payload=payload,
        symbol=getattr(payload, "symbol", None),
        product_type="SPOT",
        client_order_id=getattr(payload, "client_order_id", None),
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        with causal_command_context(envelope):
            if user.role == ROLE_BOT:
                fast_result = await service.place_order_fast(session, user, payload)
                if fast_result is not None:
                    result = fast_result
                else:
                    raise HTTPException(status_code=503, detail="fast order path unavailable")
            else:
                result = await service.place_order(session, user, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except SyntheticFlowUnavailableError as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="SYNTHETIC_FLOW_UNAVAILABLE", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="ORDER_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        if exc.status_code < 500:
            await reject_causal_command(request, envelope, code="HTTP_REJECTED", stage="INGRESS", reason=str(exc.detail))
        else:
            await mark_unknown_causal(request, envelope, reason=str(exc.detail))
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"execution exception: {exc}")
        raise


@router.post("/quote-set")
async def replace_spot_quote_set(
    payload: QuoteSetReplaceRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_quote_set_service(request)
    await enforce_private_order_rate_limit(request, user, "order_submit")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="QUOTE_SET_REPLACE",
        payload=payload,
        symbol=payload.symbol,
        product_type="SPOT",
        account_domain="SPOT",
        client_order_id=None,
        strategy_instance=payload.strategy_instance,
        generation=payload.generation,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if (user.role == ROLE_BOT) and not service.order_service._fast_writer_ready():
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        with causal_command_context(envelope):
            result = await service.submit_spot(session, user, payload)
        return matching_ack_response(await complete_causal_command(request, envelope, result, response=result))
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="QUOTE_SET_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"quote set exception: {exc.detail}")
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"quote set exception: {exc}")
        raise


@router.post("/orders/batch")
async def create_order_batch(
    payload: OrderBatchCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_submit_batch")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_PLACE_BATCH",
        payload=payload,
        product_type="SPOT",
        account_domain="SPOT",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    max_orders = settings.order_submit_batch_max_orders
    if len(payload.orders) > max_orders:
        await reject_causal_command(
            request,
            envelope,
            code="BATCH_TOO_LARGE",
            stage="INGRESS",
            reason=f"orders length cannot exceed {max_orders}",
        )
        raise HTTPException(status_code=400, detail=f"orders length cannot exceed {max_orders}")

    try:
        if user.role == ROLE_BOT:
            if not service._fast_writer_ready():
                raise HTTPException(status_code=503, detail="fast order path unavailable")
            items: list[dict] = []
            failed: list[dict] = []
            flows: list[dict] = []
            batch_market = None
            for index, order_payload in enumerate(payload.orders):
                try:
                    result = await service.place_order_fast(session, user, order_payload, broadcast=False)
                    if result is None:
                        raise HTTPException(status_code=503, detail="fast order path unavailable")
                    if batch_market is None:
                        batch_market = await service._get_market(session, order_payload.symbol)
                    flow = result.get("_fast_flow")
                    if flow is not None:
                        flows.append(flow)
                    items.append({"index": index, **result})
                except (OrderValidationError, ValueError) as exc:
                    failed.append(
                        {
                            "index": index,
                            "client_order_id": order_payload.client_order_id,
                            "symbol": order_payload.symbol,
                            "error": str(exc),
                        }
                    )
            if flows and batch_market is not None:
                await service._fast_broadcast(
                    batch_market,
                    changed_bids=[item for flow in flows for item in flow["changed_bids"]],
                    changed_asks=[item for flow in flows for item in flow["changed_asks"]],
                    trade_payloads=[item for flow in flows for item in flow["trade_payloads"]],
                )
            result = {
                "ok": not failed,
                "requested_count": len(payload.orders),
                "accepted_count": len(items),
                "failed_count": len(failed),
                "items": items,
                "failed": failed,
                "fast_path": True,
            }
            return await complete_causal_command(request, envelope, result, response=result)
        fast_result = await service.place_limit_gtc_order_batch(session, user, payload.orders)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="BATCH_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if fast_result is not None:
        return await complete_causal_command(request, envelope, fast_result, response=fast_result)

    items: list[dict] = []
    failed: list[dict] = []
    seen_reconcile_symbols: set[str] = set()
    broadcast_flows: dict[str, dict] = {}
    for index, order_payload in enumerate(payload.orders):
        try:
            symbol_key = order_payload.symbol.upper()
            result = await service.place_order(
                session,
                user,
                order_payload,
                broadcast=False,
                reconcile_book=symbol_key not in seen_reconcile_symbols,
            )
            seen_reconcile_symbols.add(symbol_key)
            flow = result.pop("_broadcast_flow", None)
            if isinstance(flow, dict):
                symbol = str(flow.get("symbol") or symbol_key)
                current = broadcast_flows.setdefault(
                    symbol,
                    {
                        "orders": [],
                        "impacted_users": set(),
                        "changed_bids": [],
                        "changed_asks": [],
                        "trade_payloads": [],
                    },
                )
                current["orders"].extend(flow.get("orders") or [])
                current["impacted_users"].update(flow.get("impacted_users") or set())
                current["changed_bids"].extend(flow.get("changed_bids") or [])
                current["changed_asks"].extend(flow.get("changed_asks") or [])
                current["trade_payloads"].extend(flow.get("trade_payloads") or [])
            items.append({"index": index, **result})
        except (OrderValidationError, ValueError) as exc:
            await session.rollback()
            failed.append(
                {
                    "index": index,
                    "client_order_id": order_payload.client_order_id,
                    "symbol": order_payload.symbol,
                    "error": str(exc),
                }
            )

    for symbol, flow in broadcast_flows.items():
        await service._broadcast_order_flow(
            session,
            symbol,
            flow["orders"],
            flow["impacted_users"],
            flow["changed_bids"],
            flow["changed_asks"],
            flow["trade_payloads"],
        )

    result = {
        "ok": not failed,
        "requested_count": len(payload.orders),
        "accepted_count": len(items),
        "failed_count": len(failed),
        "items": items,
        "failed": failed,
    }
    return await complete_causal_command(request, envelope, result, response=result)


@router.delete("/orders/{order_id}")
async def cancel_order(
    order_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_cancel")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_CANCEL",
        payload={"order_id": order_id},
        product_type="SPOT",
        account_domain="SPOT",
        client_order_id=order_id,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            fast_result = await service.cancel_order_fast(session, user, order_id)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.cancel_order(session, user, order_id)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="CANCEL_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"cancel exception: {exc.detail}")
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"cancel exception: {exc}")
        raise


@router.patch("/orders/{order_id}")
async def amend_order(
    order_id: str,
    payload: OrderAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_amend")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_AMEND",
        payload={
            "order_id": order_id,
            **(
                payload.model_dump(mode="python")
                if hasattr(payload, "model_dump")
                else dict(vars(payload))
            ),
        },
        product_type="SPOT",
        account_domain="SPOT",
        client_order_id=order_id,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            fast_result = await service.amend_order_fast(session, user, order_id, payload)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.amend_order(session, user, order_id, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="AMEND_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"amend exception: {exc.detail}")
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"amend exception: {exc}")
        raise


@router.post("/orders/amend-batch")
async def amend_order_batch(
    payload: OrderBatchAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_amend")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_AMEND_BATCH",
        payload=payload,
        symbol=payload.symbol,
        product_type="SPOT",
        account_domain="SPOT",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            if not service._fast_writer_ready():
                raise HTTPException(status_code=503, detail="fast order path unavailable")
            fast_result = await service.amend_order_batch_fast(session, user, payload)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.amend_order_batch(session, user, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="AMEND_BATCH_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"amend batch exception: {exc.detail}")
        raise


@router.post("/orders/cancel-all")
async def cancel_all(
    payload: CancelAllRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_cancel_all")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_CANCEL_ALL",
        payload=payload,
        symbol=payload.symbol,
        product_type="SPOT",
        account_domain="SPOT",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            if not service._fast_writer_ready():
                raise HTTPException(status_code=503, detail="fast order path unavailable")
            fast_result = await service.cancel_all_fast(session, user, payload.symbol)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.cancel_all(session, user, payload.symbol)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="CANCEL_ALL_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"cancel all exception: {exc.detail}")
        raise


@router.post("/orders/cancel-batch")
async def cancel_batch(
    payload: CancelBatchRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    await enforce_private_order_rate_limit(request, user, "order_cancel_batch")
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="SPOT_CANCEL_BATCH",
        payload=payload,
        symbol=payload.symbol,
        product_type="SPOT",
        account_domain="SPOT",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    symbol = payload.symbol.upper().strip()
    if user.role == ROLE_BOT:
        if not service._fast_writer_ready():
            await mark_unknown_causal(request, envelope, reason="fast order path unavailable")
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        fast_result = await service.cancel_batch_fast(session, user, symbol, payload.order_ids or [])
        if fast_result is not None:
            return await complete_causal_command(request, envelope, fast_result, response=fast_result)
        await mark_unknown_causal(request, envelope, reason="fast order path unavailable")
        raise HTTPException(status_code=503, detail="fast order path unavailable")
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        await reject_causal_command(request, envelope, code="MARKET_NOT_FOUND", stage="RISK", reason="market not found")
        raise HTTPException(status_code=404, detail="market not found")

    candidates: list[str] = []
    seen: set[str] = set()
    live_statuses = ["new", "partially_filled"]
    if payload.order_ids:
        rows = await session.execute(
            select(Order.order_id).where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.status.in_(live_statuses),
                Order.order_id.in_(payload.order_ids),
            )
        )
        selected = set(rows.scalars())
        for order_id in payload.order_ids:
            if order_id in selected and order_id not in seen:
                candidates.append(order_id)
                seen.add(order_id)

    if payload.client_order_id_prefix and len(candidates) < payload.max_orders:
        rows = await session.execute(
            select(Order.order_id)
            .where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.status.in_(live_statuses),
                Order.client_order_id.is_not(None),
                Order.client_order_id.startswith(payload.client_order_id_prefix),
            )
            .order_by(Order.created_at.asc())
            .limit(payload.max_orders)
        )
        for order_id in rows.scalars():
            if order_id not in seen:
                candidates.append(order_id)
                seen.add(order_id)
            if len(candidates) >= payload.max_orders:
                break

    canceled: list[dict] = []
    failed: list[dict] = []
    for order_id in candidates[: payload.max_orders]:
        try:
            result = await service.cancel_order(session, user, order_id)
            canceled.append(result["order"])
        except (OrderValidationError, ValueError) as exc:
            await session.rollback()
            failed.append({"order_id": order_id, "error": str(exc)})

    result = {
        "ok": not failed,
        "symbol": market.symbol,
        "requested_count": len(candidates[: payload.max_orders]),
        "canceled_count": len(canceled),
        "failed_count": len(failed),
        "orders": canceled,
        "failed": failed,
    }
    return await complete_causal_command(request, envelope, result, response=result)


@router.get("/orders/by-client-order-id/{client_order_id}")
async def get_order_by_client_order_id(
    client_order_id: str,
    request: Request,
    symbol: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    normalized_symbol = symbol.upper().strip()
    service = get_order_service(request)
    row = await session.execute(
        select(Order, Market.symbol)
        .join(Market, Market.id == Order.market_id)
        .where(
            Order.user_id == user.id,
            Order.client_order_id == client_order_id,
            Market.symbol == normalized_symbol,
        )
        .order_by(Order.created_at.desc())
        .limit(1)
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    order, market_symbol = result
    return {"item": await service.serialize_order(session, order, market_symbol)}


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    row = await session.execute(
        select(Order, Market.symbol).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    order, symbol = result
    if order.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return {"item": await service.serialize_order(session, order, symbol)}
