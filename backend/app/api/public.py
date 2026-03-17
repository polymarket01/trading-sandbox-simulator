from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.decimal_utils import decimal_to_str, quantize_scale, to_decimal
from app.core.time_utils import to_millis
from app.db.session import get_db_session
from app.models.market import Market
from app.models.trade import Trade
from app.schemas.api import MarketItem, MarketListResponse

router = APIRouter(tags=["public"])


def get_runtime(request: Request):
    return request.app.state.runtime


def normalize_market_number(value, scale: int):
    return quantize_scale(value, scale)


@router.get("/markets", response_model=MarketListResponse)
async def list_markets(session: AsyncSession = Depends(get_db_session)):
    result = await session.execute(select(Market).order_by(Market.symbol.asc()))
    items = [
        MarketItem(
            symbol=market.symbol,
            base_asset=market.base_asset,
            quote_asset=market.quote_asset,
            price_tick=normalize_market_number(market.price_tick, market.price_precision),
            qty_step=normalize_market_number(market.qty_step, market.qty_precision),
            min_qty=normalize_market_number(market.min_qty, market.qty_precision),
            min_notional=market.min_notional,
            price_precision=market.price_precision,
            qty_precision=market.qty_precision,
            is_active=market.is_active,
        )
        for market in result.scalars()
    ]
    return MarketListResponse(items=items)


@router.get("/markets/{symbol}/ticker")
async def get_ticker(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    runtime = get_runtime(request)
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    orderbook = runtime.engine.snapshot(symbol)
    stats = runtime.market_data.compute_stats(symbol, orderbook)
    trades = runtime.market_data.recent_trade_items(symbol, 1)
    last_price = to_decimal(trades[0]["price"]) if trades else to_decimal(stats["mid_price"])
    rolling_24h = await runtime.market_data.compute_24h_stats(session, market.id, last_price)
    return {
        "symbol": symbol,
        "last_price": decimal_to_str(last_price),
        "best_bid": stats["best_bid"],
        "best_ask": stats["best_ask"],
        "spread": stats["spread"],
        "spread_pct": stats["spread_pct"],
        "mid_price": stats["mid_price"],
        "depth_amount_0_5pct": stats["depth_amount_0_5pct"],
        "depth_amount_2pct": stats["depth_amount_2pct"],
        "open_24h": rolling_24h["open_24h"],
        "change_24h": rolling_24h["change_24h"],
        "change_24h_pct": rolling_24h["change_24h_pct"],
        "volume_24h": rolling_24h["volume_24h"],
        "quote_volume_24h": rolling_24h["quote_volume_24h"],
        "is_active": market.is_active,
    }


@router.get("/markets/{symbol}/orderbook")
async def get_orderbook(symbol: str, request: Request, depth: int = Query(default=20, ge=1, le=50)):
    runtime = get_runtime(request)
    orderbook = runtime.engine.snapshot(symbol, depth)
    return {
        "symbol": symbol,
        "last_update_id": runtime.market_data.seq_by_market[symbol],
        "bids": orderbook["bids"],
        "asks": orderbook["asks"],
        "ts": to_millis(datetime.now(tz=UTC)),
    }


@router.get("/markets/{symbol}/trades")
async def get_trades(symbol: str, session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=200)):
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    rows = await session.execute(
        select(Trade).where(Trade.market_id == market.id).order_by(Trade.executed_at.desc()).limit(limit)
    )
    return {
        "items": [
            {
                "trade_id": trade.trade_id,
                "price": decimal_to_str(to_decimal(trade.price)),
                "quantity": decimal_to_str(to_decimal(trade.quantity)),
                "side": trade.taker_side,
                "ts": to_millis(trade.executed_at),
            }
            for trade in rows.scalars()
        ]
    }


@router.get("/markets/{symbol}/klines")
async def get_klines(
    symbol: str,
    request: Request,
    interval: str = Query(default="1m"),
    limit: int = Query(default=200, ge=1, le=500),
):
    runtime = get_runtime(request)
    return {"items": runtime.market_data.get_klines(symbol, interval, limit)}


@router.get("/markets/{symbol}/stats")
async def get_stats(symbol: str, request: Request):
    runtime = get_runtime(request)
    orderbook = runtime.engine.snapshot(symbol)
    return runtime.market_data.compute_stats(symbol, orderbook)
