from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user
from app.core.decimal_utils import decimal_scale, quantize_scale
from app.db.session import get_db_session
from app.models.balance import Balance
from app.models.fee_profile import FeeProfile
from app.models.market import Market
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import (
    AdjustBalanceRequest,
    UpdateMarketFeesRequest,
    UpdateMarketRequest,
    UpdateUserFeesRequest,
)

router = APIRouter(tags=["admin"])


def get_service(request: Request):
    return request.app.state.order_service


def normalize_market_number(value, scale: int) -> str:
    return str(quantize_scale(value, scale))


def serialize_market(market: Market) -> dict:
    return {
        "symbol": market.symbol,
        "base_asset": market.base_asset,
        "quote_asset": market.quote_asset,
        "price_tick": normalize_market_number(market.price_tick, market.price_precision),
        "qty_step": normalize_market_number(market.qty_step, market.qty_precision),
        "min_qty": normalize_market_number(market.min_qty, market.qty_precision),
        "min_notional": str(market.min_notional),
        "price_precision": market.price_precision,
        "qty_precision": market.qty_precision,
        "is_active": market.is_active,
        "default_maker_fee_rate": str(market.default_maker_fee_rate),
        "default_taker_fee_rate": str(market.default_taker_fee_rate),
    }


@router.get("/admin/users")
async def list_users(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    users = await session.execute(select(User).order_by(User.id.asc()))
    balances = await session.execute(select(Balance))
    grouped: dict[int, list[dict]] = {}
    for item in balances.scalars():
        grouped.setdefault(item.user_id, []).append(
            {"asset": item.asset, "available": str(item.available), "frozen": str(item.frozen)}
        )
    return {
        "items": [
            {
                "id": user.id,
                "username": user.username,
                "role": user.role,
                "api_key": user.api_key,
                "api_secret": user.api_secret_hash,
                "is_active": user.is_active,
                "balances": grouped.get(user.id, []),
            }
            for user in users.scalars()
        ]
    }


@router.get("/admin/markets")
async def list_markets(
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(select(Market).order_by(Market.symbol.asc()))
    return {"items": [serialize_market(market) for market in rows.scalars()]}


@router.post("/admin/users/{user_id}/reset-balances")
async def reset_user_balances(
    user_id: int,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    await get_service(request).reset_user(session, user_id)
    return {"ok": True}


@router.post("/admin/reset-test-users")
async def reset_test_users(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(
        select(User).where(User.role.in_(["manual_user", "mm_bot"])).order_by(User.id.asc())
    )
    users = list(rows.scalars())
    service = get_service(request)
    for user in users:
        await service.reset_user(session, user.id)
    return {"ok": True, "count": len(users)}


@router.post("/admin/users/{user_id}/adjust-balance")
async def adjust_balance(
    user_id: int,
    payload: AdjustBalanceRequest,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    await get_service(request).adjust_balance(session, user_id, payload.asset, payload.amount, payload.reason)
    return {"ok": True}


@router.get("/admin/markets/{symbol}")
async def get_market(
    symbol: str,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return serialize_market(market)


@router.put("/admin/markets/{symbol}")
async def update_market(
    symbol: str,
    payload: UpdateMarketRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    changes = payload.model_dump(exclude_none=True)
    for key, value in changes.items():
        setattr(market, key, value)
    if "price_tick" in changes and "price_precision" not in changes:
        market.price_precision = decimal_scale(changes["price_tick"])
    if ("qty_step" in changes or "min_qty" in changes) and "qty_precision" not in changes:
        market.qty_precision = decimal_scale(changes.get("qty_step") or changes.get("min_qty"))
    await session.commit()
    return {"ok": True}


@router.put("/admin/markets/{symbol}/fees")
async def update_market_fees(
    symbol: str,
    payload: UpdateMarketFeesRequest,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market.default_maker_fee_rate = payload.maker_fee_rate
    market.default_taker_fee_rate = payload.taker_fee_rate
    await session.commit()
    return {"ok": True}


@router.put("/admin/users/{user_id}/fees")
async def update_user_fees(
    user_id: int,
    payload: UpdateUserFeesRequest,
    symbol: str = Query(...),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    profile = await session.scalar(
        select(FeeProfile).where(FeeProfile.user_id == user_id, FeeProfile.market_id == market.id)
    )
    if profile is None:
        profile = FeeProfile(user_id=user_id, market_id=market.id, maker_fee_rate=payload.maker_fee_rate, taker_fee_rate=payload.taker_fee_rate)
        session.add(profile)
    else:
        profile.maker_fee_rate = payload.maker_fee_rate
        profile.taker_fee_rate = payload.taker_fee_rate
    await session.commit()
    return {"ok": True}


@router.post("/admin/markets/{symbol}/reset")
async def reset_market(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return await get_service(request).reset_market(session, market)


@router.post("/admin/markets/{symbol}/wipe-data")
async def wipe_market_data(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return await get_service(request).wipe_market_data(session, market)


@router.post("/admin/ops/liquidity-state")
async def update_liquidity_state(
    payload: dict,
    request: Request,
    _: User = Depends(get_admin_user),
):
    symbol = str(payload.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    request.app.state.runtime.liquidity_metrics[symbol] = payload
    return {"ok": True}


@router.get("/ops/bots")
async def get_bot_metrics(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(select(User).where(User.role == "mm_bot").order_by(User.username.asc()))
    items = []
    for user in rows.scalars():
        open_orders = await session.scalar(
            select(func.count()).select_from(Order).where(
                Order.user_id == user.id,
                Order.status.in_(["new", "partially_filled"]),
            )
        )
        balances = await session.execute(select(Balance).where(Balance.user_id == user.id))
        last_trade = await session.execute(
            select(Trade).where(or_(Trade.taker_user_id == user.id, Trade.maker_user_id == user.id)).order_by(Trade.executed_at.desc()).limit(1)
        )
        metrics = request.app.state.runtime.bot_metrics.get(user.username, {})
        items.append(
            {
                "username": user.username,
                "api_status": "ok" if user.is_active else "paused",
                "latest_order_latency_ms": metrics.get("place_order_ms"),
                "latest_cancel_latency_ms": metrics.get("cancel_order_ms"),
                "open_order_count": open_orders or 0,
                "inventory": [{"asset": b.asset, "available": str(b.available), "frozen": str(b.frozen)} for b in balances.scalars()],
                "recent_trade_id": getattr(last_trade.scalar_one_or_none(), "trade_id", None),
                "last_heartbeat": metrics.get("last_heartbeat"),
            }
        )
    liquidity = [
        request.app.state.runtime.liquidity_metrics[symbol]
        for symbol in sorted(request.app.state.runtime.liquidity_metrics)
    ]
    return {"items": items, "liquidity": liquidity}
