from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ROLE_ADMIN, ROLE_BOT, ROLE_MANUAL, SIDE_BUY, SIDE_SELL, TIF_GTC, ZERO
from app.models.balance import Balance
from app.models.fee_profile import FeeProfile
from app.models.kline import Kline
from app.models.ledger_entry import LedgerEntry
from app.models.market import Market
from app.models.order import Order
from app.models.reset_template import ResetTemplate
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import OrderCreateRequest
from app.services.ids import next_ledger_id
from app.services.order_service import OrderService
from app.services.runtime import AppRuntime


BOT_USERS = [
    (f"spot_mm_{index}", ROLE_BOT, f"mm{index}-demo-key", f"mm{index}-demo-secret")
    for index in range(1, 11)
]

FLOW_USERS = [
    ("flow_user_1", ROLE_BOT, "flow1-demo-key", "flow1-demo-secret"),
    ("flow_user_2", ROLE_BOT, "flow2-demo-key", "flow2-demo-secret"),
]

USERS = [
    ("spot_manual_user", ROLE_MANUAL, "manual-demo-key", "manual-demo-secret"),
    *BOT_USERS,
    *FLOW_USERS,
    ("spot_admin", ROLE_ADMIN, "admin-demo-key", "admin-demo-secret"),
]

MARKETS = [
    {
        "symbol": "BTCUSDT",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.1"),
        "qty_step": Decimal("0.0001"),
        "min_qty": Decimal("0.0001"),
        "min_notional": Decimal("5"),
        "price_precision": 1,
        "qty_precision": 4,
        "default_maker_fee_rate": Decimal("-0.00005"),
        "default_taker_fee_rate": Decimal("0.00070"),
    },
    {
        "symbol": "XXXUSDT",
        "base_asset": "XXX",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 3,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("-0.00005"),
        "default_taker_fee_rate": Decimal("0.00080"),
    },
    {
        "symbol": "YYYUSDT",
        "base_asset": "YYY",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 3,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("-0.00005"),
        "default_taker_fee_rate": Decimal("0.00080"),
    },
    {
        "symbol": "ZZZUSDT",
        "base_asset": "ZZZ",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.0001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 4,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("-0.00005"),
        "default_taker_fee_rate": Decimal("0.00080"),
    },
]

DEFAULT_TEST_ASSETS = {
    "USDT": Decimal("100000000"),
    "BTC": Decimal("100000000"),
    "XXX": Decimal("100000000"),
    "YYY": Decimal("100000000"),
    "ZZZ": Decimal("100000000"),
}

RESET_AMOUNTS = {
    username: DEFAULT_TEST_ASSETS.copy()
    for username, role, _, _ in USERS
    if role in {ROLE_MANUAL, ROLE_BOT}
}

MARKET_SEEDS = {
    "BTCUSDT": {
        "base_price": Decimal("62000"),
        "book_gap": Decimal("5"),
        "book_qty_base": Decimal("0.12"),
        "book_qty_step": Decimal("0.01"),
        "history_tick": Decimal("0.1"),
        "history_qty_base": Decimal("0.08"),
        "history_qty_step": Decimal("0.01"),
    },
    "XXXUSDT": {
        "base_price": Decimal("12"),
        "book_gap": Decimal("0.01"),
        "book_qty_base": Decimal("800"),
        "book_qty_step": Decimal("30"),
        "history_tick": Decimal("0.001"),
        "history_qty_base": Decimal("600"),
        "history_qty_step": Decimal("25"),
    },
    "YYYUSDT": {
        "base_price": Decimal("4.2"),
        "book_gap": Decimal("0.01"),
        "book_qty_base": Decimal("1500"),
        "book_qty_step": Decimal("45"),
        "history_tick": Decimal("0.001"),
        "history_qty_base": Decimal("1200"),
        "history_qty_step": Decimal("35"),
    },
    "ZZZUSDT": {
        "base_price": Decimal("0.2450"),
        "book_gap": Decimal("0.0005"),
        "book_qty_base": Decimal("5000"),
        "book_qty_step": Decimal("120"),
        "history_tick": Decimal("0.0001"),
        "history_qty_base": Decimal("4200"),
        "history_qty_step": Decimal("90"),
    },
}


async def bootstrap(session: AsyncSession, runtime: AppRuntime) -> None:
    users = await _ensure_users(session)
    markets = await _ensure_markets(session)
    await _ensure_fee_profiles(session, users, markets)
    await _ensure_reset_templates(session, users)
    await _ensure_initial_balances(session, users)
    await session.commit()

    service = OrderService(runtime)
    trade_count = await session.scalar(select(func.count()).select_from(Trade))
    if not trade_count:
        await session.execute(delete(Balance))
        await session.execute(delete(LedgerEntry))
        await session.execute(delete(Order))
        await session.execute(delete(Trade))
        await session.execute(delete(Kline))
        await session.commit()
        await _ensure_initial_balances(session, users)
        await session.commit()
        await _seed_trade_history(session, users, service)

    open_order_count = await session.scalar(
        select(func.count()).select_from(Order).where(Order.status.in_(["new", "partially_filled"]))
    )
    if not open_order_count:
        await _seed_orderbook(session, users, service)


async def _ensure_users(session: AsyncSession) -> dict[str, User]:
    existing = await session.execute(select(User))
    users = {user.username: user for user in existing.scalars()}
    for username, role, api_key, api_secret in USERS:
        if username in users:
            continue
        user = User(
            username=username,
            role=role,
            api_key=api_key,
            api_secret_hash=api_secret,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        users[username] = user
    return users


async def _ensure_markets(session: AsyncSession) -> dict[str, Market]:
    existing = await session.execute(select(Market))
    markets = {market.symbol: market for market in existing.scalars()}
    for payload in MARKETS:
        if payload["symbol"] in markets:
            continue
        market = Market(**payload, is_active=True)
        session.add(market)
        await session.flush()
        markets[market.symbol] = market
    return markets


async def _ensure_fee_profiles(session: AsyncSession, users: dict[str, User], markets: dict[str, Market]) -> None:
    existing = await session.execute(select(FeeProfile))
    keys = {(item.user_id, item.market_id) for item in existing.scalars()}
    for username, user in users.items():
        if user.role == ROLE_ADMIN:
            continue
        for market in markets.values():
            key = (user.id, market.id)
            if key in keys:
                continue
            if user.role == ROLE_BOT:
                maker = Decimal("-0.00010")
                taker = Decimal("0.00060")
            else:
                maker = Decimal("0.00020")
                taker = Decimal("0.00100")
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=maker,
                    taker_fee_rate=taker,
                )
            )


async def _ensure_reset_templates(session: AsyncSession, users: dict[str, User]) -> None:
    existing = await session.execute(select(ResetTemplate))
    existing_map = {(item.name, item.user_id, item.asset): item for item in existing.scalars()}
    for username, assets in RESET_AMOUNTS.items():
        user = users[username]
        for asset, amount in assets.items():
            key = ("default", user.id, asset)
            template = existing_map.get(key)
            if template is None:
                session.add(ResetTemplate(name="default", user_id=user.id, asset=asset, amount=amount))
            else:
                template.amount = amount


async def _ensure_initial_balances(session: AsyncSession, users: dict[str, User]) -> None:
    existing = await session.execute(select(Balance))
    existing_keys = {(item.user_id, item.asset) for item in existing.scalars()}
    now = datetime.now(tz=UTC)
    for username, assets in RESET_AMOUNTS.items():
        user = users[username]
        for asset, amount in assets.items():
            if (user.id, asset) in existing_keys:
                continue
            session.add(Balance(user_id=user.id, asset=asset, available=amount, frozen=ZERO, updated_at=now))
            session.add(
                LedgerEntry(
                    entry_id=next_ledger_id(),
                    user_id=user.id,
                    asset=asset,
                    change_type="deposit_reset",
                    amount=amount,
                    balance_before=ZERO,
                    balance_after=amount,
                    available_before=ZERO,
                    available_after=amount,
                    frozen_before=ZERO,
                    frozen_after=ZERO,
                    note="bootstrap",
                    created_at=now,
                )
            )


async def _seed_orderbook(session: AsyncSession, users: dict[str, User], service: OrderService) -> None:
    placements = []
    for offset in range(1, 26):
        for symbol, seed in MARKET_SEEDS.items():
            bid = seed["base_price"] - seed["book_gap"] * offset
            ask = seed["base_price"] + seed["book_gap"] * offset
            qty = seed["book_qty_base"] + seed["book_qty_step"] * offset
            placements.extend(
                [
                    (users["spot_mm_1"], symbol, SIDE_BUY, bid, qty),
                    (users["spot_mm_2"], symbol, SIDE_SELL, ask, qty),
                ]
            )

    for user, symbol, side, price, quantity in placements:
        request = OrderCreateRequest(
            symbol=symbol,
            side=side,
            type="limit",
            tif=TIF_GTC,
            price=price,
            quantity=quantity,
            client_order_id=f"seed-{symbol}-{side}-{price}",
        )
        await service.place_order(session, user, request)


async def _seed_trade_history(session: AsyncSession, users: dict[str, User], service: OrderService) -> None:
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=180)
    for symbol, seed in MARKET_SEEDS.items():
        for idx in range(360):
            ts = start + timedelta(seconds=30 * idx)
            wave = Decimal(str((idx % 24) - 12)) * seed["history_tick"]
            price = seed["base_price"] + wave
            quantity = seed["history_qty_base"] + (Decimal(idx % 7) * seed["history_qty_step"])
            # 交替买卖方，避免 mm1 始终买入耗尽 USDT
            taker_side = SIDE_BUY
            maker_side = SIDE_SELL
            if idx % 2 == 0:
                maker_user = users["spot_mm_2"]
                taker_user = users["spot_mm_1"]
            else:
                maker_user = users["spot_mm_1"]
                taker_user = users["spot_mm_2"]

            maker_request = OrderCreateRequest(
                symbol=symbol,
                side=maker_side,
                type="limit",
                tif=TIF_GTC,
                price=price,
                quantity=quantity,
                client_order_id=f"seed-maker-{symbol}-{idx}",
            )
            taker_request = OrderCreateRequest(
                symbol=symbol,
                side=taker_side,
                type="limit",
                tif=TIF_GTC,
                price=price,
                quantity=quantity,
                client_order_id=f"seed-taker-{symbol}-{idx}",
            )
            await service.place_order(session, maker_user, maker_request, now=ts)
            await service.place_order(session, taker_user, taker_request, now=ts + timedelta(milliseconds=10))
