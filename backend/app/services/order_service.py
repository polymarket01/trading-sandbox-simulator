from __future__ import annotations

import time
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    ORDER_STATUS_REJECTED,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_MARKET_PROTECTED,
    ROLE_BOT,
    SIDE_BUY,
    SIDE_SELL,
    TIF_GTC,
    ZERO,
)
from app.core.decimal_utils import decimal_to_str, is_step_aligned, quantize_scale, to_decimal
from app.core.time_utils import ensure_utc, to_millis
from app.models.balance import Balance
from app.models.fee_profile import FeeProfile
from app.models.ledger_entry import LedgerEntry
from app.models.market import Market
from app.models.order import Order
from app.models.kline import Kline
from app.models.reset_template import ResetTemplate
from app.models.trade import Trade
from app.models.user import User
from app.services.ids import next_order_id, next_trade_id
from app.services.matching_engine import BookOrder
from app.services.runtime import AppRuntime


class OrderValidationError(Exception):
    pass


@dataclass(slots=True)
class ReservePlan:
    asset: str
    amount: Decimal


class OrderService:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _price_tick(market: Market) -> Decimal:
        return quantize_scale(market.price_tick, market.price_precision)

    @staticmethod
    def _qty_step(market: Market) -> Decimal:
        return quantize_scale(market.qty_step, market.qty_precision)

    @staticmethod
    def _min_qty(market: Market) -> Decimal:
        return quantize_scale(market.min_qty, market.qty_precision)

    @classmethod
    def _normalize_price(cls, market: Market, value: Decimal | str | int | float | None) -> Decimal | None:
        if value is None:
            return None
        return quantize_scale(value, market.price_precision)

    @classmethod
    def _normalize_qty(cls, market: Market, value: Decimal | str | int | float) -> Decimal:
        return quantize_scale(value, market.qty_precision)

    def _normalize_payload(self, market: Market, payload) -> None:
        payload.quantity = self._normalize_qty(market, payload.quantity)
        if payload.price is not None:
            payload.price = self._normalize_price(market, payload.price)

    async def load_open_orders(self, session: AsyncSession) -> None:
        needs_commit = False
        result = await session.execute(
            select(Order, Market.symbol)
            .join(Market, Market.id == Order.market_id)
            .where(
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif == TIF_GTC,
            )
        )
        for order, symbol in result.all():
            market = await session.scalar(select(Market).where(Market.symbol == symbol))
            if market is None:
                continue
            remaining = self._normalize_qty(market, order.remaining_quantity)
            price = self._normalize_price(market, order.price)
            order.quantity = self._normalize_qty(market, order.quantity)
            order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
            order.remaining_quantity = remaining
            if price is not None:
                order.price = price
            if remaining <= ZERO or price is None:
                order.remaining_quantity = ZERO
                order.status = ORDER_STATUS_FILLED if Decimal(order.filled_quantity) > ZERO else ORDER_STATUS_CANCELED
                needs_commit = True
                continue
            self.runtime.engine.load_resting_order(
                symbol,
                BookOrder(
                    order_id=order.order_id,
                    user_id=order.user_id,
                    side=order.side,
                    price=price,
                    remaining=remaining,
                    created_at=ensure_utc(order.created_at),
                ),
            )
        if needs_commit:
            await session.commit()

    async def place_order(self, session: AsyncSession, user: User, payload, *, now: datetime | None = None) -> dict:
        started = time.perf_counter()
        market = await self._get_market(session, payload.symbol)
        order_id = next_order_id()
        now = now or datetime.now(tz=UTC)
        lock = self.runtime.market_locks[market.symbol]
        self._normalize_payload(market, payload)

        async with lock:
            try:
                reference_price, max_price, min_price = await self._validate_request(session, user, market, payload)
            except OrderValidationError as exc:
                order = await self._create_order_record(
                    session,
                    user=user,
                    market=market,
                    order_id=order_id,
                    payload=payload,
                    status=ORDER_STATUS_REJECTED,
                    created_at=now,
                    reference_price=None,
                    max_price=None,
                    min_price=None,
                    reject_reason=str(exc),
                )
                await session.commit()
                return {"order": await self.serialize_order(session, order, market.symbol)}

            reserve = self._build_reserve_plan(market, payload, reference_price, max_price, min_price)
            if reserve.amount > ZERO:
                try:
                    await self.runtime.account_service.reserve(
                        session,
                        user.id,
                        reserve.asset,
                        reserve.amount,
                        related_order_id=order_id,
                        note=f"{payload.type}:{payload.side}",
                        created_at=now,
                    )
                except ValueError as exc:
                    raise OrderValidationError(f"insufficient available balance for asset {reserve.asset}") from exc

            order = await self._create_order_record(
                session,
                user=user,
                market=market,
                order_id=order_id,
                payload=payload,
                status=ORDER_STATUS_NEW,
                created_at=now,
                reference_price=reference_price,
                max_price=max_price,
                min_price=min_price,
                reject_reason=None,
            )
            result = self.runtime.engine.process_order(
                symbol=market.symbol,
                order_id=order.order_id,
                user_id=user.id,
                side=payload.side,
                quantity=Decimal(order.quantity),
                created_at=now,
                limit_price=Decimal(order.price) if order.price is not None else None,
                can_rest=payload.type == ORDER_TYPE_LIMIT and payload.tif == TIF_GTC,
                max_price=max_price,
                min_price=min_price,
            )

            maker_order_ids = {fill.maker_order_id for fill in result.fills}
            maker_orders = await self._load_orders_map(session, maker_order_ids)
            impacted_users = {user.id}
            updated_orders: dict[str, Order] = {order.order_id: order}
            trade_payloads: list[dict] = []
            total_notional = ZERO

            taker_fee_rate = await self._get_fee_rate(session, user.id, market.id, taker=True, market=market)
            maker_fee_rates: dict[int, Decimal] = {}

            for fill in result.fills:
                maker_order = maker_orders[fill.maker_order_id]
                if maker_order.user_id not in maker_fee_rates:
                    maker_fee_rates[maker_order.user_id] = await self._get_fee_rate(
                        session,
                        maker_order.user_id,
                        market.id,
                        taker=False,
                        market=market,
                    )
                trade = await self._apply_trade(
                    session=session,
                    market=market,
                    taker_user=user,
                    taker_order=order,
                    maker_order=maker_order,
                    quantity=fill.quantity,
                    price=fill.price,
                    taker_fee_rate=taker_fee_rate,
                    maker_fee_rate=maker_fee_rates[maker_order.user_id],
                    executed_at=now,
                )
                total_notional += Decimal(trade.quote_amount)
                impacted_users.add(maker_order.user_id)
                updated_orders[maker_order.order_id] = maker_order
                self.runtime.market_data.ingest_trade(
                    market.symbol,
                    price=Decimal(trade.price),
                    quantity=Decimal(trade.quantity),
                    side=payload.side,
                    ts=now,
                    trade_id=trade.trade_id,
                )
                trade_payloads.append(await self.serialize_trade(trade, market.symbol))
                await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "1m")
                await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "5m")

            order.notional = total_notional
            order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
            order.avg_price = (total_notional / Decimal(order.filled_quantity)) if Decimal(order.filled_quantity) > ZERO else None
            order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
            order.updated_at = now

            if Decimal(order.filled_quantity) == ZERO:
                if result.placed_on_book:
                    order.status = ORDER_STATUS_NEW
                else:
                    order.status = ORDER_STATUS_CANCELED if payload.type != ORDER_TYPE_LIMIT or payload.tif != TIF_GTC else ORDER_STATUS_NEW
            elif Decimal(order.remaining_quantity) == ZERO:
                order.status = ORDER_STATUS_FILLED
            elif result.placed_on_book:
                order.status = ORDER_STATUS_PARTIALLY_FILLED
            else:
                order.status = ORDER_STATUS_CANCELED
                order.canceled_at = now

            await self._release_order_leftover(session, market, order, reserve, now)
            await session.flush()
            await session.commit()

        await self._broadcast_order_flow(
            session,
            market.symbol,
            list(updated_orders.values()),
            impacted_users,
            result.changed_bids,
            result.changed_asks,
            trade_payloads,
        )
        self._update_metric(user, "place_order_ms", started)
        return {"order": await self.serialize_order(session, order, market.symbol), "trades": trade_payloads}

    async def cancel_order(self, session: AsyncSession, user: User, order_id: str, *, admin_override: bool = False) -> dict:
        result = await session.execute(
            select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
        )
        record = result.first()
        if record is None:
            raise OrderValidationError("order not found")
        order, market = record
        if not admin_override and order.user_id != user.id:
            raise OrderValidationError("cannot cancel others order")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            raise OrderValidationError("order is not cancelable")

        started = time.perf_counter()
        async with self.runtime.market_locks[market.symbol]:
            _, _, changes = self.runtime.engine.cancel_order(market.symbol, order.order_id)
            now = datetime.now(tz=UTC)
            await self._release_remaining_for_cancel(session, market, order, now)
            order.status = ORDER_STATUS_CANCELED
            order.canceled_at = now
            order.updated_at = now
            await session.commit()

        changed_bids = changes if order.side == SIDE_BUY else []
        changed_asks = changes if order.side == SIDE_SELL else []
        await self._broadcast_order_flow(session, market.symbol, [order], {order.user_id}, changed_bids, changed_asks, [])
        self._update_metric(user, "cancel_order_ms", started)
        return {"order": await self.serialize_order(session, order, market.symbol)}

    async def amend_order(self, session: AsyncSession, user: User, order_id: str, payload, *, admin_override: bool = False) -> dict:
        result = await session.execute(
            select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
        )
        record = result.first()
        if record is None:
            raise OrderValidationError("order not found")
        order, market = record
        if not admin_override and order.user_id != user.id:
            raise OrderValidationError("cannot amend others order")
        if order.type != ORDER_TYPE_LIMIT or order.tif != TIF_GTC:
            raise OrderValidationError("only gtc limit orders are amendable")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            raise OrderValidationError("order is not amendable")

        started = time.perf_counter()
        now = datetime.now(tz=UTC)
        lock = self.runtime.market_locks[market.symbol]

        async with lock:
            new_price = self._normalize_price(market, payload.price if payload.price is not None else order.price)
            new_quantity = self._normalize_qty(market, payload.quantity)
            filled_quantity = self._normalize_qty(market, order.filled_quantity)
            current_quantity = self._normalize_qty(market, order.quantity)
            current_price = self._normalize_price(market, order.price)
            current_remaining = self._normalize_qty(market, order.remaining_quantity)

            if current_price is None or new_price is None:
                raise OrderValidationError("limit order price missing")

            new_remaining = self._normalize_qty(market, new_quantity - filled_quantity)
            await self._validate_amend_request(
                session,
                user,
                market,
                order,
                new_price=new_price,
                new_quantity=new_quantity,
                new_remaining=new_remaining,
            )

            if new_price == current_price and new_quantity == current_quantity:
                return {"order": await self.serialize_order(session, order, market.symbol), "kept_priority": True}

            await self._adjust_resting_reserve(
                session,
                market,
                order,
                old_price=current_price,
                old_remaining=current_remaining,
                new_price=new_price,
                new_remaining=new_remaining,
                now=now,
            )

            amend = self.runtime.engine.amend_order(market.symbol, order.order_id, new_price, new_remaining)
            if amend is None:
                raise OrderValidationError("order not found on book")

            order.price = new_price
            order.quantity = new_quantity
            order.remaining_quantity = new_remaining
            order.updated_at = now
            order.status = ORDER_STATUS_PARTIALLY_FILLED if filled_quantity > ZERO else ORDER_STATUS_NEW
            await session.commit()

        await self._broadcast_order_flow(
            session,
            market.symbol,
            [order],
            {order.user_id},
            amend.changed_bids,
            amend.changed_asks,
            [],
        )
        self._update_metric(user, "amend_order_ms", started)
        return {"order": await self.serialize_order(session, order, market.symbol), "kept_priority": amend.kept_priority}

    async def cancel_all(
        self,
        session: AsyncSession,
        user: User,
        symbol: str,
        *,
        admin_override: bool = False,
        target_user_id: int | None = None,
    ) -> dict:
        market = await self._get_market(session, symbol)
        async with self.runtime.market_locks[market.symbol]:
            stmt = select(Order).where(
                Order.market_id == market.id,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            )
            if target_user_id is not None:
                stmt = stmt.where(Order.user_id == target_user_id)
            elif not admin_override:
                stmt = stmt.where(Order.user_id == user.id)
            rows = await session.execute(stmt)
            orders = list(rows.scalars())
            now = datetime.now(tz=UTC)
            changed_bids: list[list[str]] = []
            changed_asks: list[list[str]] = []
            impacted_users: set[int] = set()
            for order in orders:
                _, _, changes = self.runtime.engine.cancel_order(market.symbol, order.order_id)
                if order.side == SIDE_BUY:
                    changed_bids.extend(changes)
                else:
                    changed_asks.extend(changes)
                await self._release_remaining_for_cancel(session, market, order, now)
                order.status = ORDER_STATUS_CANCELED
                order.canceled_at = now
                order.updated_at = now
                impacted_users.add(order.user_id)
            await session.commit()

        for order in orders:
            await self._broadcast_order_flow(session, market.symbol, [order], {order.user_id}, changed_bids, changed_asks, [])
        return {"count": len(orders)}

    async def reset_market(self, session: AsyncSession, market: Market) -> dict:
        admin = User(id=0, username="system", role="admin", api_key=None, api_secret_hash=None, is_active=True)
        response = await self.cancel_all(session, admin, market.symbol, admin_override=True)
        return response

    async def wipe_market_data(self, session: AsyncSession, market: Market) -> dict:
        async with self.runtime.market_locks[market.symbol]:
            order_rows = await session.execute(select(Order.order_id).where(Order.market_id == market.id))
            order_ids = [item[0] for item in order_rows.all()]
            trade_rows = await session.execute(select(Trade.trade_id).where(Trade.market_id == market.id))
            trade_ids = [item[0] for item in trade_rows.all()]

            deleted_ledger = 0
            if order_ids or trade_ids:
                ledger_stmt = delete(LedgerEntry).where(
                    or_(
                        LedgerEntry.related_order_id.in_(order_ids) if order_ids else false(),
                        LedgerEntry.related_trade_id.in_(trade_ids) if trade_ids else false(),
                    )
                )
                ledger_result = await session.execute(ledger_stmt)
                deleted_ledger = ledger_result.rowcount or 0

            order_result = await session.execute(delete(Order).where(Order.market_id == market.id))
            trade_result = await session.execute(delete(Trade).where(Trade.market_id == market.id))
            kline_result = await session.execute(delete(Kline).where(Kline.market_id == market.id))
            await self._rebuild_balances_from_ledger(session)
            await session.commit()

        self.runtime.engine.books.pop(market.symbol, None)
        self.runtime.market_data.recent_trades.pop(market.symbol, None)
        self.runtime.market_data.live_klines.pop(market.symbol, None)
        self.runtime.market_data.latest_stats.pop(market.symbol, None)
        self.runtime.market_data.seq_by_market[market.symbol] = 0
        return {
            "ok": True,
            "symbol": market.symbol,
            "deleted_orders": order_result.rowcount or 0,
            "deleted_trades": trade_result.rowcount or 0,
            "deleted_klines": kline_result.rowcount or 0,
            "deleted_ledger_entries": deleted_ledger,
        }

    async def reset_user(self, session: AsyncSession, user_id: int) -> None:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        markets_result = await session.execute(select(Market).order_by(Market.symbol.asc()))
        markets = list(markets_result.scalars())
        now = datetime.now(tz=UTC)

        async with AsyncExitStack() as stack:
            for market in markets:
                await stack.enter_async_context(self.runtime.market_locks[market.symbol])

            open_rows = await session.execute(
                select(Order, Market)
                .join(Market, Market.id == Order.market_id)
                .where(
                    Order.user_id == user_id,
                    Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                )
                .order_by(Market.symbol.asc(), Order.created_at.asc())
            )
            open_orders = list(open_rows.all())
            for order, market in open_orders:
                self.runtime.engine.cancel_order(market.symbol, order.order_id)
                order.status = ORDER_STATUS_CANCELED
                order.canceled_at = now
                order.updated_at = now

            await self.runtime.account_service.reset_balances(session, user_id, "default")
            await session.commit()

        balances = await self.serialize_balances(session, user_id)
        await self.runtime.ws.broadcast_private(
            user_id,
            "balances",
            {"channel": "balances", "type": "update", "data": balances, "ts": to_millis(datetime.now(tz=UTC))},
        )
        for order, market in open_orders:
            await self.runtime.ws.broadcast_private(
                user_id,
                "orders",
                {"channel": "orders", "type": "update", "data": await self.serialize_order(session, order, market.symbol)},
            )

    async def adjust_balance(self, session: AsyncSession, user_id: int, asset: str, amount: Decimal, reason: str) -> None:
        now = datetime.now(tz=UTC)
        await self.runtime.account_service.apply_change(
            session,
            user_id,
            asset,
            available_delta=amount,
            frozen_delta=ZERO,
            change_type="manual_adjustment",
            note=reason,
            amount=amount,
            created_at=now,
        )
        await session.commit()

    async def _rebuild_balances_from_ledger(self, session: AsyncSession) -> None:
        rows = await session.execute(select(LedgerEntry).order_by(LedgerEntry.created_at.asc(), LedgerEntry.id.asc()))
        totals: dict[tuple[int, str], tuple[Decimal, Decimal]] = defaultdict(lambda: (ZERO, ZERO))
        for entry in rows.scalars():
            key = (entry.user_id, entry.asset)
            available_delta = Decimal(entry.available_after) - Decimal(entry.available_before)
            frozen_delta = Decimal(entry.frozen_after) - Decimal(entry.frozen_before)
            current_available, current_frozen = totals[key]
            totals[key] = (current_available + available_delta, current_frozen + frozen_delta)

        templates = await session.execute(select(ResetTemplate.user_id, ResetTemplate.asset))
        for user_id, asset in templates.all():
            totals.setdefault((user_id, asset), (ZERO, ZERO))

        await session.execute(delete(Balance))
        now = datetime.now(tz=UTC)
        for (user_id, asset), (available, frozen) in totals.items():
            session.add(
                Balance(
                    user_id=user_id,
                    asset=asset,
                    available=available,
                    frozen=frozen,
                    updated_at=now,
                )
            )
        await session.flush()

    async def serialize_order(self, session: AsyncSession, order: Order, symbol: str | None = None) -> dict:
        market_symbol = symbol
        if market_symbol is None:
            row = await session.execute(select(Market.symbol).where(Market.id == order.market_id))
            market_symbol = row.scalar_one()
        market_row = await session.execute(select(Market).where(Market.symbol == market_symbol))
        market = market_row.scalar_one()
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "symbol": market_symbol,
            "side": order.side,
            "type": order.type,
            "tif": order.tif,
            "status": order.status,
            "price": decimal_to_str(self._normalize_price(market, order.price)) if order.price is not None else None,
            "quantity": decimal_to_str(self._normalize_qty(market, order.quantity)),
            "filled_quantity": decimal_to_str(self._normalize_qty(market, order.filled_quantity)),
            "remaining_quantity": decimal_to_str(self._normalize_qty(market, order.remaining_quantity)),
            "avg_price": decimal_to_str(to_decimal(order.avg_price)) if order.avg_price is not None else None,
            "notional": decimal_to_str(to_decimal(order.notional)) if order.notional is not None else None,
            "reference_price": decimal_to_str(to_decimal(order.reference_price)) if order.reference_price is not None else None,
            "protection_bps": order.protection_bps,
            "max_price": decimal_to_str(to_decimal(order.max_price)) if order.max_price is not None else None,
            "min_price": decimal_to_str(to_decimal(order.min_price)) if order.min_price is not None else None,
            "reject_reason": order.reject_reason,
            "created_at": to_millis(order.created_at),
            "updated_at": to_millis(order.updated_at),
        }

    async def serialize_trade(self, trade: Trade, symbol: str) -> dict:
        return {
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "price": decimal_to_str(to_decimal(trade.price)),
            "quantity": decimal_to_str(to_decimal(trade.quantity)),
            "quote_amount": decimal_to_str(to_decimal(trade.quote_amount)),
            "taker_side": trade.taker_side,
            "maker_fee": decimal_to_str(to_decimal(trade.maker_fee)),
            "taker_fee": decimal_to_str(to_decimal(trade.taker_fee)),
            "maker_fee_asset": trade.fee_asset_maker,
            "taker_fee_asset": trade.fee_asset_taker,
            "executed_at": to_millis(trade.executed_at),
        }

    async def serialize_balances(self, session: AsyncSession, user_id: int) -> list[dict]:
        rows = await session.execute(
            select(Balance).where(Balance.user_id == user_id).order_by(Balance.asset.asc())
        )
        return [
            {
                "asset": item.asset,
                "available": decimal_to_str(to_decimal(item.available)),
                "frozen": decimal_to_str(to_decimal(item.frozen)),
            }
            for item in rows.scalars()
        ]

    async def serialize_ledger_entries(self, session: AsyncSession, user_id: int, asset: str | None, limit: int) -> list[dict]:
        stmt = select(LedgerEntry).where(LedgerEntry.user_id == user_id)
        if asset:
            stmt = stmt.where(LedgerEntry.asset == asset)
        stmt = stmt.order_by(LedgerEntry.created_at.desc()).limit(limit)
        rows = await session.execute(stmt)
        return [
            {
                "entry_id": entry.entry_id,
                "asset": entry.asset,
                "change_type": entry.change_type,
                "amount": decimal_to_str(to_decimal(entry.amount)),
                "available_before": decimal_to_str(to_decimal(entry.available_before)),
                "available_after": decimal_to_str(to_decimal(entry.available_after)),
                "frozen_before": decimal_to_str(to_decimal(entry.frozen_before)),
                "frozen_after": decimal_to_str(to_decimal(entry.frozen_after)),
                "balance_before": decimal_to_str(to_decimal(entry.balance_before)),
                "balance_after": decimal_to_str(to_decimal(entry.balance_after)),
                "related_order_id": entry.related_order_id,
                "related_trade_id": entry.related_trade_id,
                "note": entry.note,
                "created_at": to_millis(entry.created_at),
            }
            for entry in rows.scalars()
        ]

    async def _get_market(self, session: AsyncSession, symbol: str) -> Market:
        result = await session.execute(select(Market).where(Market.symbol == symbol))
        market = result.scalar_one_or_none()
        if market is None:
            raise OrderValidationError("market not found")
        return market

    async def _validate_request(self, session: AsyncSession, user: User, market: Market, payload) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        quantity = self._normalize_qty(market, payload.quantity)
        qty_step = self._qty_step(market)
        min_qty = self._min_qty(market)
        price_tick = self._price_tick(market)
        if not market.is_active:
            raise OrderValidationError("market is inactive")
        if quantity <= ZERO:
            raise OrderValidationError("quantity must be positive")
        if not is_step_aligned(quantity, qty_step):
            raise OrderValidationError("quantity does not match qty_step")
        if quantity < min_qty:
            raise OrderValidationError("quantity below min_qty")

        reference_price = None
        max_price = None
        min_price = None

        if payload.type == ORDER_TYPE_LIMIT:
            price = self._normalize_price(market, payload.price)
            assert price is not None
            if price <= ZERO:
                raise OrderValidationError("price must be positive")
            if not is_step_aligned(price, price_tick):
                raise OrderValidationError("price does not match tick")
            if price * quantity < Decimal(market.min_notional):
                raise OrderValidationError("notional below min_notional")
        else:
            reference_price = self.runtime.engine.reference_price(market.symbol)
            if reference_price is None:
                raise OrderValidationError("no liquidity on book")
            executable_qty, executable_notional = self.runtime.engine.simulate_cost(
                market.symbol,
                payload.side,
                quantity,
            )
            if executable_qty > ZERO and executable_notional < Decimal(market.min_notional):
                raise OrderValidationError("notional below min_notional")
            if payload.type == ORDER_TYPE_MARKET_PROTECTED:
                bps = Decimal(payload.protection_bps) / Decimal("10000")
                if payload.side == SIDE_BUY:
                    max_price = reference_price * (Decimal("1") + bps)
                else:
                    min_price = reference_price * (Decimal("1") - bps)

        reserve = self._build_reserve_plan(market, payload, reference_price, max_price, min_price)
        if reserve.amount > ZERO:
            await self.runtime.account_service.ensure_available(session, user.id, reserve.asset, reserve.amount)
        return reference_price, max_price, min_price

    async def _validate_amend_request(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order: Order,
        *,
        new_price: Decimal,
        new_quantity: Decimal,
        new_remaining: Decimal,
    ) -> None:
        qty_step = self._qty_step(market)
        min_qty = self._min_qty(market)
        price_tick = self._price_tick(market)
        filled_quantity = self._normalize_qty(market, order.filled_quantity)
        current_price = self._normalize_price(market, order.price)
        current_remaining = self._normalize_qty(market, order.remaining_quantity)

        if not market.is_active:
            raise OrderValidationError("market is inactive")
        if new_quantity <= ZERO:
            raise OrderValidationError("quantity must be positive")
        if new_price <= ZERO:
            raise OrderValidationError("price must be positive")
        if not is_step_aligned(new_quantity, qty_step):
            raise OrderValidationError("quantity does not match qty_step")
        if not is_step_aligned(new_price, price_tick):
            raise OrderValidationError("price does not match tick")
        if new_quantity < filled_quantity:
            raise OrderValidationError("quantity cannot be below filled quantity")
        if new_remaining <= ZERO:
            raise OrderValidationError("quantity must exceed filled quantity")
        if new_remaining < min_qty:
            raise OrderValidationError("remaining quantity below min_qty")
        if new_price * new_remaining < Decimal(market.min_notional):
            raise OrderValidationError("notional below min_notional")

        old_reserve = self._resting_reserve_amount(market, order.side, current_price, current_remaining)
        new_reserve = self._resting_reserve_amount(market, order.side, new_price, new_remaining)
        if new_reserve > old_reserve:
            await self.runtime.account_service.ensure_available(
                session,
                user.id,
                self._resting_reserve_asset(market, order.side),
                new_reserve - old_reserve,
            )

    def _build_reserve_plan(self, market: Market, payload, reference_price: Decimal | None, max_price: Decimal | None, min_price: Decimal | None) -> ReservePlan:
        quantity = Decimal(payload.quantity)
        if payload.side == SIDE_SELL:
            return ReservePlan(asset=market.base_asset, amount=quantity)
        if payload.type == ORDER_TYPE_LIMIT:
            return ReservePlan(asset=market.quote_asset, amount=Decimal(payload.price) * quantity)
        filled_qty, notional = self.runtime.engine.simulate_cost(
            market.symbol,
            payload.side,
            quantity,
            max_price=max_price,
            min_price=min_price,
        )
        if filled_qty == ZERO and reference_price is not None and payload.type == ORDER_TYPE_MARKET:
            return ReservePlan(asset=market.quote_asset, amount=ZERO)
        return ReservePlan(asset=market.quote_asset, amount=notional)

    @staticmethod
    def _resting_reserve_asset(market: Market, side: str) -> str:
        return market.quote_asset if side == SIDE_BUY else market.base_asset

    @staticmethod
    def _resting_reserve_amount(market: Market, side: str, price: Decimal, remaining: Decimal) -> Decimal:
        return price * remaining if side == SIDE_BUY else remaining

    async def _adjust_resting_reserve(
        self,
        session: AsyncSession,
        market: Market,
        order: Order,
        *,
        old_price: Decimal,
        old_remaining: Decimal,
        new_price: Decimal,
        new_remaining: Decimal,
        now: datetime,
    ) -> None:
        asset = self._resting_reserve_asset(market, order.side)
        old_amount = self._resting_reserve_amount(market, order.side, old_price, old_remaining)
        new_amount = self._resting_reserve_amount(market, order.side, new_price, new_remaining)
        delta = new_amount - old_amount
        if delta > ZERO:
            await self.runtime.account_service.reserve(
                session,
                order.user_id,
                asset,
                delta,
                related_order_id=order.order_id,
                note="amend_increase_reserve",
                created_at=now,
            )
        elif delta < ZERO:
            await self.runtime.account_service.release(
                session,
                order.user_id,
                asset,
                -delta,
                related_order_id=order.order_id,
                note="amend_release_reserve",
                created_at=now,
            )

    async def _create_order_record(
        self,
        session: AsyncSession,
        *,
        user: User,
        market: Market,
        order_id: str,
        payload,
        status: str,
        created_at: datetime,
        reference_price: Decimal | None,
        max_price: Decimal | None,
        min_price: Decimal | None,
        reject_reason: str | None,
    ) -> Order:
        order = Order(
            order_id=order_id,
            client_order_id=payload.client_order_id,
            user_id=user.id,
            market_id=market.id,
            side=payload.side,
            type=payload.type,
            tif=payload.tif,
            status=status,
            price=payload.price,
            quantity=payload.quantity,
            filled_quantity=ZERO,
            avg_price=None,
            notional=ZERO,
            remaining_quantity=payload.quantity,
            reference_price=reference_price,
            protection_bps=payload.protection_bps,
            max_price=max_price,
            min_price=min_price,
            reject_reason=reject_reason,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(order)
        await session.flush()
        return order

    async def _load_orders_map(self, session: AsyncSession, order_ids: set[str]) -> dict[str, Order]:
        if not order_ids:
            return {}
        rows = await session.execute(select(Order).where(Order.order_id.in_(order_ids)))
        return {order.order_id: order for order in rows.scalars()}

    async def _get_fee_rate(self, session: AsyncSession, user_id: int, market_id: int, *, taker: bool, market: Market) -> Decimal:
        row = await session.execute(
            select(FeeProfile).where(FeeProfile.user_id == user_id, FeeProfile.market_id == market_id)
        )
        profile = row.scalar_one_or_none()
        if profile is None:
            return Decimal(market.default_taker_fee_rate if taker else market.default_maker_fee_rate)
        return Decimal(profile.taker_fee_rate if taker else profile.maker_fee_rate)

    def _fee_amount(self, side: str, rate: Decimal, quantity: Decimal, quote_amount: Decimal, market: Market) -> tuple[Decimal, str]:
        if side == SIDE_BUY:
            return quantity * rate, market.base_asset
        return quote_amount * rate, market.quote_asset

    async def _apply_trade(
        self,
        *,
        session: AsyncSession,
        market: Market,
        taker_user: User,
        taker_order: Order,
        maker_order: Order,
        quantity: Decimal,
        price: Decimal,
        taker_fee_rate: Decimal,
        maker_fee_rate: Decimal,
        executed_at: datetime,
    ) -> Trade:
        quote_amount = quantity * price
        maker_fee, maker_fee_asset = self._fee_amount(maker_order.side, maker_fee_rate, quantity, quote_amount, market)
        taker_fee, taker_fee_asset = self._fee_amount(taker_order.side, taker_fee_rate, quantity, quote_amount, market)

        trade = Trade(
            trade_id=next_trade_id(),
            market_id=market.id,
            price=price,
            quantity=quantity,
            quote_amount=quote_amount,
            taker_order_id=taker_order.order_id,
            maker_order_id=maker_order.order_id,
            taker_user_id=taker_user.id,
            maker_user_id=maker_order.user_id,
            taker_side=taker_order.side,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            fee_asset_maker=maker_fee_asset,
            fee_asset_taker=taker_fee_asset,
            executed_at=executed_at,
        )
        session.add(trade)

        await self._settle_side(
            session=session,
            market=market,
            user_id=taker_user.id,
            order=taker_order,
            quantity=quantity,
            price=price,
            fee=taker_fee,
            fee_asset=taker_fee_asset,
            trade_id=trade.trade_id,
            executed_at=executed_at,
        )
        await self._settle_side(
            session=session,
            market=market,
            user_id=maker_order.user_id,
            order=maker_order,
            quantity=quantity,
            price=price,
            fee=maker_fee,
            fee_asset=maker_fee_asset,
            trade_id=trade.trade_id,
            executed_at=executed_at,
        )

        for current in (taker_order, maker_order):
            current.filled_quantity = self._normalize_qty(market, Decimal(current.filled_quantity) + quantity)
            current.remaining_quantity = self._normalize_qty(market, Decimal(current.remaining_quantity) - quantity)
            current.notional = Decimal(current.notional or ZERO) + quote_amount
            current.avg_price = Decimal(current.notional) / Decimal(current.filled_quantity)
            current.updated_at = executed_at
            if current.remaining_quantity <= ZERO:
                current.remaining_quantity = ZERO
                current.status = ORDER_STATUS_FILLED
            else:
                current.status = ORDER_STATUS_PARTIALLY_FILLED

        await session.flush()
        return trade

    async def _settle_side(
        self,
        *,
        session: AsyncSession,
        market: Market,
        user_id: int,
        order: Order,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fee_asset: str,
        trade_id: str,
        executed_at: datetime,
    ) -> None:
        quote_amount = quantity * price
        account = self.runtime.account_service
        if order.side == SIDE_BUY:
            reserve_price = Decimal(order.price) if order.type == ORDER_TYPE_LIMIT and order.price is not None else price
            reserve_consumed = reserve_price * quantity
            refund = reserve_consumed - quote_amount
            await account.apply_change(
                session,
                user_id,
                market.quote_asset,
                available_delta=refund,
                frozen_delta=-reserve_consumed,
                change_type="trade_settlement",
                related_order_id=order.order_id,
                related_trade_id=trade_id,
                note="quote_spend",
                amount=-quote_amount,
                created_at=executed_at,
            )
            await account.apply_change(
                session,
                user_id,
                market.base_asset,
                available_delta=quantity,
                frozen_delta=ZERO,
                change_type="trade_settlement",
                related_order_id=order.order_id,
                related_trade_id=trade_id,
                note="base_receive",
                amount=quantity,
                created_at=executed_at,
            )
        else:
            await account.apply_change(
                session,
                user_id,
                market.base_asset,
                available_delta=ZERO,
                frozen_delta=-quantity,
                change_type="trade_settlement",
                related_order_id=order.order_id,
                related_trade_id=trade_id,
                note="base_spend",
                amount=-quantity,
                created_at=executed_at,
            )
            await account.apply_change(
                session,
                user_id,
                market.quote_asset,
                available_delta=quote_amount,
                frozen_delta=ZERO,
                change_type="trade_settlement",
                related_order_id=order.order_id,
                related_trade_id=trade_id,
                note="quote_receive",
                amount=quote_amount,
                created_at=executed_at,
            )

        if fee != ZERO:
            await account.apply_change(
                session,
                user_id,
                fee_asset,
                available_delta=-fee,
                frozen_delta=ZERO,
                change_type="fee",
                related_order_id=order.order_id,
                related_trade_id=trade_id,
                note="trade_fee",
                amount=-fee,
                created_at=executed_at,
            )

    async def _release_order_leftover(self, session: AsyncSession, market: Market, order: Order, reserve: ReservePlan, now: datetime) -> None:
        remaining = self._normalize_qty(market, order.remaining_quantity)
        if remaining <= ZERO:
            return
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED} and order.type == ORDER_TYPE_LIMIT and order.tif == TIF_GTC:
            return
        if order.side == SIDE_BUY:
            if order.type == ORDER_TYPE_LIMIT and order.price is not None:
                release_amount = Decimal(order.price) * remaining
            else:
                release_amount = reserve.amount - Decimal(order.notional or ZERO)
        else:
            release_amount = remaining
        await self.runtime.account_service.release(
            session,
            order.user_id,
            reserve.asset,
            max(release_amount, ZERO),
            related_order_id=order.order_id,
            note="order_leftover_release",
            created_at=now,
        )

    async def _release_remaining_for_cancel(self, session: AsyncSession, market: Market, order: Order, now: datetime) -> None:
        remaining = self._normalize_qty(market, order.remaining_quantity)
        if remaining <= ZERO:
            return
        if order.side == SIDE_BUY:
            release = (Decimal(order.price) * remaining) if order.price is not None else ZERO
            asset = market.quote_asset
        else:
            release = remaining
            asset = market.base_asset
        await self.runtime.account_service.release(
            session,
            order.user_id,
            asset,
            release,
            related_order_id=order.order_id,
            note="cancel_release",
            created_at=now,
        )

    async def _broadcast_order_flow(
        self,
        session: AsyncSession,
        symbol: str,
        orders: list[Order],
        impacted_users: set[int],
        changed_bids: list[list[str]],
        changed_asks: list[list[str]],
        trade_payloads: list[dict],
    ) -> None:
        # Public WS subscribers can watch up to 50 levels. Broadcasting a full
        # snapshot here avoids shallow-book delta drift on the frontend, where
        # removed top levels otherwise cannot promote deeper levels into view.
        snapshot = self.runtime.engine.snapshot(symbol, 50)
        seq = self.runtime.market_data.next_seq(symbol)
        if changed_bids or changed_asks:
            await self.runtime.ws.broadcast_public(
                "orderbook",
                symbol,
                {
                    "channel": "orderbook",
                    "type": "snapshot",
                    "symbol": symbol,
                    "seq": seq,
                    "bids": snapshot["bids"],
                    "asks": snapshot["asks"],
                    "ts": to_millis(datetime.now(tz=UTC)),
                },
            )
        stats = self.runtime.market_data.compute_stats(symbol, snapshot)
        await self.runtime.ws.broadcast_public(
            "stats",
            symbol,
            {"channel": "stats", "type": "update", "symbol": symbol, "data": stats},
        )
        if trade_payloads:
            await self.runtime.ws.broadcast_public(
                "trades",
                symbol,
                {"channel": "trades", "type": "update", "symbol": symbol, "items": trade_payloads},
            )
            for interval in ["1s", "5s", "15s", "1m", "5m", "15m"]:
                items = self.runtime.market_data.get_klines(symbol, interval, 1)
                if items:
                    await self.runtime.ws.broadcast_public(
                        "kline",
                        symbol,
                        {
                            "channel": "kline",
                            "type": "update",
                            "symbol": symbol,
                            "interval": interval,
                            "kline": items[-1],
                        },
                        interval=interval,
                    )
        market_symbol = symbol
        for user_id in impacted_users:
            balances = await self.serialize_balances(session, user_id)
            await self.runtime.ws.broadcast_private(
                user_id,
                "balances",
                {"channel": "balances", "type": "update", "data": balances, "ts": to_millis(datetime.now(tz=UTC))},
            )
            for relevant in orders:
                if relevant.user_id != user_id:
                    continue
                await self.runtime.ws.broadcast_private(
                    user_id,
                    "orders",
                    {"channel": "orders", "type": "update", "data": await self.serialize_order(session, relevant, market_symbol)},
                )

    def _update_metric(self, user: User, metric: str, started: float) -> None:
        if user.role != ROLE_BOT:
            return
        self.runtime.bot_metrics[user.username][metric] = round((time.perf_counter() - started) * 1000, 2)
        self.runtime.bot_metrics[user.username]["last_heartbeat"] = to_millis(datetime.now(tz=UTC))
