from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.time_utils import to_millis
from app.db.session import get_db_session
from app.models.market import Market
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import CancelAllRequest, OrderAmendRequest, OrderCreateRequest
from app.services.order_service import OrderService, OrderValidationError

router = APIRouter(tags=["private"])


def get_runtime(request: Request):
    return request.app.state.runtime


def get_order_service(request: Request) -> OrderService:
    return request.app.state.order_service


@router.get("/account/balances")
async def get_balances(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    return {"items": await service.serialize_balances(session, user.id)}


@router.get("/account/orders/open")
async def get_open_orders(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    stmt = select(Order, Market.symbol).join(Market, Market.id == Order.market_id).where(
        Order.user_id == user.id,
        Order.status.in_(["new", "partially_filled"]),
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol)
    rows = await session.execute(stmt.order_by(Order.created_at.desc()))
    items = [await service.serialize_order(session, order, market_symbol) for order, market_symbol in rows.all()]
    return {"items": items}


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
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    stmt = (
        select(Trade, Market.symbol)
        .join(Market, Market.id == Trade.market_id)
        .where(or_(Trade.taker_user_id == user.id, Trade.maker_user_id == user.id))
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol)
    rows = await session.execute(stmt.order_by(Trade.executed_at.desc()).limit(limit))
    items = []
    for trade, market_symbol in rows.all():
        is_taker = trade.taker_user_id == user.id
        side = trade.taker_side if is_taker else ("sell" if trade.taker_side == "buy" else "buy")
        items.append(
            {
                "trade_id": trade.trade_id,
                "symbol": market_symbol,
                "side": side,
                "price": str(trade.price),
                "quantity": str(trade.quantity),
                "quote_amount": str(trade.quote_amount),
                "fee": str(trade.taker_fee if is_taker else trade.maker_fee),
                "liquidity_role": "taker" if is_taker else "maker",
                "fee_asset": trade.fee_asset_taker if is_taker else trade.fee_asset_maker,
                "ts": to_millis(trade.executed_at),
            }
        )
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
    try:
        return await service.place_order(session, user, payload)
    except OrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/orders/{order_id}")
async def cancel_order(
    order_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    try:
        return await service.cancel_order(session, user, order_id)
    except OrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/orders/{order_id}")
async def amend_order(
    order_id: str,
    payload: OrderAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    try:
        return await service.amend_order(session, user, order_id, payload)
    except OrderValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/orders/cancel-all")
async def cancel_all(
    payload: CancelAllRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_order_service(request)
    return await service.cancel_all(session, user, payload.symbol)


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    row = await session.execute(
        select(Order, Market.symbol).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    order, symbol = result
    if order.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="forbidden")
    return {"item": await get_order_service(request).serialize_order(session, order, symbol)}
