from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from sqlalchemy import select

from app.api import admin, private, public
from app.core.config import settings
from app.core.security import verify_ws_signature
from app.core.time_utils import to_millis
from app.db.session import SessionLocal
from app.models.market import Market
from app.models.order import Order
from app.models.user import User
from app.seed.bootstrap import bootstrap
from app.services.order_service import OrderService
from app.services.runtime import AppRuntime


async def stats_loop(app: FastAPI) -> None:
    while True:
        try:
            runtime: AppRuntime = app.state.runtime
            for symbol in list(runtime.engine.books.keys()):
                snapshot = runtime.engine.snapshot(symbol)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime = AppRuntime()
    order_service = OrderService(runtime)
    app.state.runtime = runtime
    app.state.order_service = order_service
    async with SessionLocal() as session:
        await bootstrap(session, runtime)
        markets = await session.execute(select(Market))
        for market in markets.scalars():
            loaded = await runtime.market_data.load_from_trades(session, market.id, market.symbol)
            if not loaded:
                await runtime.market_data.load_persisted_klines(session, market.id, market.symbol)
        if not runtime.engine.books:
            await order_service.load_open_orders(session)
    task = asyncio.create_task(stats_loop(app))
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(default_response_class=ORJSONResponse, lifespan=lifespan, title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(public.router, prefix=settings.api_prefix)
app.include_router(private.router, prefix=settings.api_prefix)
app.include_router(admin.router, prefix=settings.api_prefix)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


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
            await runtime.ws.subscribe_public(websocket, channel, symbol, interval=interval)
            if channel == "orderbook":
                snapshot = runtime.engine.snapshot(symbol, message.get("depth", 20))
                await websocket.send_json(
                    {
                        "channel": "orderbook",
                        "type": "snapshot",
                        "symbol": symbol,
                        "seq": runtime.market_data.next_seq(symbol),
                        "bids": snapshot["bids"],
                        "asks": snapshot["asks"],
                        "ts": to_millis(datetime.now(tz=UTC)),
                    }
                )
            elif channel == "trades":
                await websocket.send_json(
                    {
                        "channel": "trades",
                        "type": "update",
                        "symbol": symbol,
                        "items": runtime.market_data.recent_trade_items(symbol, 50),
                    }
                )
            elif channel == "kline":
                items = runtime.market_data.get_klines(symbol, interval or "1m", 1)
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
                stats = runtime.market_data.compute_stats(symbol, runtime.engine.snapshot(symbol))
                await websocket.send_json({"channel": "stats", "type": "update", "symbol": symbol, "data": stats})
    except WebSocketDisconnect:
        await runtime.ws.disconnect(websocket)


@app.websocket("/ws/private")
async def ws_private(websocket: WebSocket):
    runtime: AppRuntime = websocket.app.state.runtime
    await runtime.ws.register(websocket)
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("op") == "auth":
                async with SessionLocal() as session:
                    user = await session.scalar(select(User).where(User.api_key == message.get("api_key")))
                if user is None:
                    await websocket.send_json({"type": "error", "detail": "invalid api key"})
                    continue
                timestamp = int(message.get("timestamp", 0))
                if abs(to_millis(datetime.now(tz=UTC)) - timestamp) > settings.default_ws_signature_ttl_ms:
                    await websocket.send_json({"type": "error", "detail": "timestamp expired"})
                    continue
                if not verify_ws_signature(user.api_key or "", user.api_secret_hash or "", timestamp, message.get("signature", "")):
                    await websocket.send_json({"type": "error", "detail": "signature invalid"})
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
                        rows = await session.execute(
                            select(Order, Market.symbol)
                            .join(Market, Market.id == Order.market_id)
                            .where(Order.user_id == user_id)
                            .order_by(Order.updated_at.desc())
                            .limit(20)
                        )
                        items = [await service.serialize_order(session, order, symbol) for order, symbol in rows.all()]
                        await websocket.send_json({"channel": "orders", "type": "snapshot", "items": items})
    except WebSocketDisconnect:
        await runtime.ws.disconnect(websocket)
