from __future__ import annotations

from app.services.matching_faults import BusinessRejected, NotExecuted

from app.services.maker_permissions import min_notional_exempt

import asyncio
import logging
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    ORDER_STATUS_REJECTED,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    ORDER_TYPE_MARKET_PROTECTED,
    PRODUCT_TYPE_SPOT,
    ROLE_BOT,
    SIDE_BUY,
    SIDE_SELL,
    TIF_GTC,
    TIF_POST_ONLY,
    ZERO,
)
from app.core.decimal_utils import decimal_to_str, is_step_aligned, quantize_scale, to_decimal
from app.core.time_utils import ensure_utc, to_millis
from app.models.balance import Balance
from app.models.display_kline import DisplayKline
from app.models.fee_profile import FeeProfile
from app.models.ledger_entry import LedgerEntry
from app.models.market import Market
from app.models.order import Order
from app.models.kline import Kline
from app.models.reset_template import ResetTemplate
from app.models.trade import Trade
from app.models.user import User
from app.services.ids import next_order_id, next_trade_id
from app.services.matching_engine import BookOrder, EngineResult, MatchFill
from app.services.market_data_service import BOOTSTRAP_SEED_SOURCE
from app.services.financial_outbox_service import enqueue_financial_outbox
from app.services.runtime import AppRuntime
from app.services.latest_wins_scheduler import LatestWinsTaskScheduler
from app.services.sequencer import (
    AmendOrderCommand,
    BatchAmendCommand,
    BatchAmendItem,
    CancelOrderCommand,
    CrossingAmendCommand,
    NewOrderCommand,
)
from app.services.synthetic_flow_service import (
    FLOW_ORDER_PREFIXES,
    SyntheticFlowValidationError,
    SyntheticFlowUnavailableError,
    broadcast_synthetic_flow,
    execute_synthetic_flow,
)
from app.services.persistence_contract import (
    is_flow_client_order_id,
    is_paper_robot_topup_eligible,
    is_runtime_only_mode,
    platform_durable_contract,

)
from exchange_common.quote_pipeline import SelfTradePolicy


class OrderValidationError(BusinessRejected):
    pass


BOT_ORDER_PREFIXES = ("mmv2-", "repairv2-", "perpmm-", "perpresearch-")
SEED_ORDER_PREFIXES = ("seed-",)
BOT_BALANCE_BROADCAST_MIN_INTERVAL_MS = 1000


def _opt_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def engine_result_from_plan(plan: dict) -> EngineResult:
    result = EngineResult(
        remaining_quantity=Decimal(str(plan["remaining_quantity"])),
        placed_on_book=bool(plan.get("placed_on_book", False)),
        stop_reason=plan.get("stop_reason"),
        stp_action=plan.get("stp_action"),
        stp_reason=plan.get("stp_reason"),
        stp_intercept_count=int(plan.get("stp_intercept_count", 0) or 0),
        stp_decremented_quantity=Decimal(str(plan.get("stp_decremented_quantity", "0") or "0")),
    )
    for item in plan.get("fills", []) or []:
        result.fills.append(
            MatchFill(
                maker_order_id=str(item["maker_order_id"]),
                maker_user_id=int(item["maker_user_id"]),
                price=Decimal(str(item["price"])),
                quantity=Decimal(str(item["quantity"])),
            )
        )
    result.changed_bids = [[str(px), str(qty)] for px, qty in plan.get("changed_bids", []) or []]
    result.changed_asks = [[str(px), str(qty)] for px, qty in plan.get("changed_asks", []) or []]
    return result


@dataclass(slots=True)
class ReservePlan:
    asset: str
    amount: Decimal


class OrderService:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime
        self._last_bot_balance_broadcast_ms: dict[int, int] = defaultdict(int)
        self._fast_orders: dict[str, dict] = {}
        self._fast_client_ids: dict[tuple[int, int, str], str] = {}
        self._fee_rate_cache: dict[tuple[int, int, bool], Decimal] = {}
        self._fast_state_metrics: dict[str, int] = {
            "ephemeral_ghost_reconciliations": 0,
            "ephemeral_ghost_evictions": 0,
            "ephemeral_ghost_release_events": 0,
            "ephemeral_ghost_release_shortfalls": 0,
        }
        self._fast_broadcast_scheduler = LatestWinsTaskScheduler(
            self._consume_fast_broadcast,
            name="spot-fast-broadcast",
        )

    async def _consume_fast_broadcast(self, _symbol: str, payload: dict) -> None:
        await self._deferred_fast_broadcast(**payload)

    @staticmethod
    def _merge_fast_broadcast_payload(previous: dict, latest: dict) -> dict:
        merged = dict(previous)
        # Keep the first pre-change book and the latest published book.  The
        # public stream is latest-wins; private/trade payloads are retained in
        # a bounded tail for REST-recoverable telemetry.
        merged["snapshot"] = latest["snapshot"]
        merged["seq"] = latest["seq"]
        merged["updated_at_ms"] = latest["updated_at_ms"]
        merged["changed_bids"] = latest["changed_bids"]
        merged["changed_asks"] = latest["changed_asks"]
        merged["trade_payloads"] = (list(previous.get("trade_payloads") or []) + list(latest.get("trade_payloads") or []))[-512:]
        return merged

    def fast_broadcast_metrics(self) -> dict[str, int]:
        return self._fast_broadcast_scheduler.metrics_snapshot()

    @staticmethod
    def _stp_context(user: User) -> dict:
        is_bot = user.role == ROLE_BOT
        return {
            "stp_account_key": f"user:{int(user.id)}",
            "stp_group_key": "bot" if is_bot else "user",
            "stp_is_bot": is_bot,
            "stp_mode": str(settings.stp_same_account_mode),
            "stp_policy": SelfTradePolicy(
                same_account_mode=settings.stp_same_account_mode,
                bot_cross_mode=settings.stp_bot_cross_mode,
            ),
        }

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
        # Integer markets reject fractions before normalization; never change the requested amount.
        if market.qty_precision == 0 and to_decimal(payload.quantity) != to_decimal(payload.quantity).to_integral_value():
            raise OrderValidationError("quantity must be an integer for this market")
        if market.price_precision == 0 and payload.price is not None and to_decimal(payload.price) != to_decimal(payload.price).to_integral_value():
            raise OrderValidationError("price must be an integer for this market")
        payload.quantity = self._normalize_qty(market, payload.quantity)
        if payload.price is not None:
            payload.price = self._normalize_price(market, payload.price)

    async def load_open_orders(self, session: AsyncSession) -> None:
        query = (
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
                Market.product_type == PRODUCT_TYPE_SPOT,
                Order.product_type == PRODUCT_TYPE_SPOT,
            )
            .order_by(Order.sequence_number.asc(), Order.created_at.asc(), Order.id.asc())
        )
        if settings.persistence_mode == "memory":
            query = query.join(User, User.id == Order.user_id).where(User.role != "mm_bot")
        result = await session.execute(query)
        for order, market in result.all():
            remaining = self._normalize_qty(market, order.remaining_quantity)
            price = self._normalize_price(market, order.price)
            order.quantity = self._normalize_qty(market, order.quantity)
            order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
            order.remaining_quantity = remaining
            self.runtime.observe_sequence(market.symbol, order.sequence_number)
            if price is not None:
                order.price = price
            if remaining <= ZERO or price is None:
                continue
            self.runtime.engine.load_resting_order(
                market.symbol,
                BookOrder(
                    order_id=order.order_id,
                    user_id=order.user_id,
                    side=order.side,
                    price=price,
                    remaining=remaining,
                    created_at=ensure_utc(order.created_at),
                    sequence_number=int(order.sequence_number or 0),
                    stp_account_key=f"user:{int(order.user_id)}",
                    stp_group_key="bot" if str(order.order_id).startswith(BOT_ORDER_PREFIXES) else "user",
                    stp_is_bot=str(order.order_id).startswith(BOT_ORDER_PREFIXES),
                    stp_mode=settings.stp_same_account_mode,
                ),
            )

    async def _open_order_ids_for_market(self, session: AsyncSession, market: Market) -> set[str]:
        rows = await session.execute(
            select(Order.order_id).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_SPOT,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
            )
        )
        return set(rows.scalars())

    async def _spot_financial_keys(self, session: AsyncSession, market: Market, *extra_user_ids: int) -> list[str]:
        rows = await session.execute(
            select(Order.user_id).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_SPOT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            ).distinct()
        )
        user_ids = {int(user_id) for user_id in rows.scalars()}
        user_ids.update(int(user_id) for user_id in extra_user_ids)
        return [
            f"spot:{user_id}:{asset}"
            for user_id in sorted(user_ids)
            for asset in sorted({market.base_asset, market.quote_asset})
        ]

    async def _load_live_client_order(self, session: AsyncSession, user: User, market: Market, client_order_id: str | None) -> Order | None:
        if not client_order_id:
            return None
        return await session.scalar(
            select(Order)
            .where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.client_order_id == client_order_id,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1)
        )

    async def _reconcile_engine_book(self, session: AsyncSession, market: Market) -> tuple[list[list[str]], list[list[str]], list[str]]:
        if is_runtime_only_mode():
            # Robot quote orders are in-memory only; pruning the engine against
            # the DB would tear down the live book by design.
            return [], [], []
        writer = getattr(self.runtime, "persistence_writer", None)
        if writer is not None:
            lag = getattr(writer, "materialization_lag", None)
            if (writer._busy or not writer._queue.empty()) or (
                callable(lag) and lag() > 0
            ):
                # The DB snapshot is incomplete while events are still being
                # materialized; pruning against it would drop just-placed
                # fast-path orders from the engine.
                return [], [], []
        valid_order_ids = await self._open_order_ids_for_market(session, market)
        if platform_durable_contract():
            # 策略机器人报价是重启重建的临时态，耐用 DB 只保留生命周期锚点；
            # 以 DB 开放订单为准裁剪引擎会误删仍在快速镜像中的策略报价，
            # 导致策略进程的 QuoteSet 撤单永久失败（镜像有单、引擎无单）。
            # Paper 模式下把快速镜像里的活跃机器人报价并入有效集合。
            valid_order_ids = set(valid_order_ids)
            for order_id, snap in self._fast_orders.items():
                if (
                    int(snap.get("market_id") or -1) == int(market.id)
                    and str(snap.get("status") or "") in {"new", "partially_filled"}
                ):
                    valid_order_ids.add(str(order_id))
        changed_bids, changed_asks, removed = self.runtime.engine.reconcile_open_orders(market.symbol, valid_order_ids)
        if removed:
            self.runtime.orderbook_snapshot_unlocked(market.symbol, 50)
            self.runtime.record_orderbook_reconcile(
                market.symbol,
                reason="spot_engine_only_prune",
                engine_only_removed=len(removed),
            )
        return changed_bids, changed_asks, removed

    async def _load_book_orders_from_db(self, session: AsyncSession, market: Market) -> list[BookOrder]:
        rows = await session.execute(
            select(Order)
            .where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_SPOT,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
            )
            .order_by(Order.sequence_number.asc(), Order.created_at.asc(), Order.id.asc())
        )
        book_orders: list[BookOrder] = []
        for order in rows.scalars():
            remaining = self._normalize_qty(market, order.remaining_quantity)
            price = self._normalize_price(market, order.price)
            self.runtime.observe_sequence(market.symbol, order.sequence_number)
            if remaining <= ZERO or price is None:
                continue
            book_orders.append(
                BookOrder(
                    order_id=order.order_id,
                    user_id=order.user_id,
                    side=order.side,
                    price=price,
                    remaining=remaining,
                    created_at=ensure_utc(order.created_at),
                    sequence_number=int(order.sequence_number or 0),
                    stp_account_key=f"user:{int(order.user_id)}",
                    stp_group_key="bot" if str(order.order_id).startswith(BOT_ORDER_PREFIXES) else "user",
                    stp_is_bot=str(order.order_id).startswith(BOT_ORDER_PREFIXES),
                    stp_mode=settings.stp_same_account_mode,
                )
            )
        return book_orders

    async def _rebuild_engine_book_from_db(self, session: AsyncSession, market_id: int, *, reason: str) -> dict:
        writer = getattr(self.runtime, "persistence_writer", None)
        catchup = getattr(writer, "materialize_catchup", None)
        if callable(catchup):
            await catchup(timeout=10.0)
        market = await session.scalar(select(Market).where(Market.id == market_id))
        if market is None:
            return {}
        book_orders = await self._load_book_orders_from_db(session, market)
        if platform_durable_contract():
            # 重建引擎时同样要把快速镜像里的活跃机器人报价放回盘口：
            # 耐用 DB 只保留报价生命周期锚点，不含全部临时档位。
            existing = {str(item.order_id) for item in book_orders}
            for snap in self._fast_orders.values():
                if (
                    int(snap.get("market_id") or -1) != int(market.id)
                    or str(snap.get("status") or "") not in {"new", "partially_filled"}
                    or str(snap.get("order_id") or "") in existing
                ):
                    continue
                price = self._normalize_price(market, snap.get("price"))
                remaining = self._normalize_qty(market, snap.get("remaining_quantity"))
                if price is None or price <= ZERO or remaining <= ZERO:
                    continue
                created = snap.get("created_at")
                created_at = datetime.fromtimestamp(int(created) / 1000, tz=UTC) if created is not None else datetime.now(tz=UTC)
                book_orders.append(
                    BookOrder(
                        order_id=str(snap["order_id"]),
                        user_id=int(snap.get("user_id") or 0),
                        side=str(snap.get("side") or ""),
                        price=price,
                        remaining=remaining,
                        created_at=created_at,
                        sequence_number=int(snap.get("sequence_number") or 0),
                        stp_account_key=f"user:{int(snap.get('user_id') or 0)}",
                        stp_group_key="bot",
                        stp_is_bot=True,
                        stp_mode=settings.stp_same_account_mode,
                    )
                )
                existing.add(str(snap["order_id"]))
        recovery = self.runtime.engine.rebuild_market(market.symbol, book_orders)
        self._last_book_recovery = {"symbol": market.symbol, **recovery}
        self.runtime.orderbook_snapshot_unlocked(market.symbol, 50)
        return self.runtime.record_orderbook_reconcile(
            market.symbol,
            reason=reason,
            db_reloaded=len(book_orders),
        )

    async def _commit_or_rebuild_engine(self, session: AsyncSession, market: Market, *, reason: str) -> None:
        market_id = int(market.id)
        try:
            await session.commit()
            self.runtime.publish_orderbook_snapshot_unlocked(market.symbol)
        except Exception:
            await session.rollback()
            await self._rebuild_engine_book_from_db(session, market_id, reason=reason)
            raise

    async def _flush_commit_or_rebuild_engine(self, session: AsyncSession, market: Market, *, reason: str) -> None:
        market_id = int(market.id)
        try:
            await session.flush()
            await session.commit()
            self.runtime.publish_orderbook_snapshot_unlocked(market.symbol)
        except Exception:
            await session.rollback()
            await self._rebuild_engine_book_from_db(session, market_id, reason=reason)
            raise

    async def place_order(
        self,
        session: AsyncSession,
        user: User,
        payload,
        *,
        now: datetime | None = None,
        broadcast: bool = True,
        reconcile_book: bool = True,
    ) -> dict:
        started = time.perf_counter()
        market = await self._get_market(session, payload.symbol)
        market_id = int(market.id)
        order_id = next_order_id()
        now = now or datetime.now(tz=UTC)
        self._normalize_payload(market, payload)

        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self._spot_financial_keys(session, market, user.id)
        ):
            if reconcile_book:
                await self._reconcile_engine_book(session, market)
            existing_order = await self._load_live_client_order(session, user, market, payload.client_order_id)
            if existing_order is not None:
                self._update_metric(user, "place_order_ms", started)
                return {"order": await self.serialize_order(session, existing_order, market.symbol), "idempotent": True}
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
            sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                NewOrderCommand(
                    order_id=order.order_id,
                    user_id=user.id,
                    side=payload.side,
                    quantity=Decimal(order.quantity),
                    created_at=now,
                    limit_price=Decimal(order.price) if order.price is not None else None,
                    can_rest=payload.type == ORDER_TYPE_LIMIT and payload.tif in {TIF_GTC, TIF_POST_ONLY},
                    max_price=max_price,
                    min_price=min_price,
                    **self._stp_context(user),
                )
            )
            result = sequencer_result.engine_result
            if result is None:
                raise OrderValidationError("sequencer did not return engine result")
            order.sequence_number = sequencer_result.sequence_number

            updated_orders, impacted_users, trade_payloads, total_notional = await self._finalize_placed_order(
                session=session,
                market=market,
                user=user,
                order=order,
                payload=payload,
                result=result,
                reserve=reserve,
                sequence_number=sequencer_result.sequence_number,
                now=now,
                commit=True,
            )

        broadcast_flow = {
            "symbol": market.symbol,
            "orders": list(updated_orders.values()),
            "impacted_users": impacted_users,
            "changed_bids": result.changed_bids,
            "changed_asks": result.changed_asks,
            "trade_payloads": trade_payloads,
        }
        if broadcast:
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
        response = {"order": await self.serialize_order(session, order, market.symbol), "trades": trade_payloads}
        if not broadcast:
            response["_broadcast_flow"] = broadcast_flow
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            snap = self._fast_order_snapshot(order, market)
            self._fast_orders[order.order_id] = snap
            self._fast_register_client_id(int(user.id), int(market.id), payload.client_order_id, order.order_id)
        elif payload.client_order_id:
            self._fast_unregister_client_id(int(user.id), int(market.id), payload.client_order_id, order.order_id)
        return response

    async def _finalize_placed_order(
        self,
        *,
        session: AsyncSession,
        market: Market,
        user: User,
        order: Order,
        payload,
        result,
        reserve: ReservePlan,
        sequence_number: int,
        now: datetime,
        commit: bool,
        ingest: bool = True,
    ) -> tuple[dict[str, Order], set[int], list[dict], Decimal]:
        """Common settlement tail shared by the sync path and the write-behind replay."""
        market_id = int(market.id)
        try:
            updated_orders, impacted_users, trade_payloads, total_notional = await self._apply_engine_fills(
                session=session,
                market=market,
                taker_user=user,
                taker_order=order,
                taker_side=payload.side,
                result=result,
                executed_at=now,
                ingest=ingest,
            )
        except Exception:
            if commit:
                # The in-memory engine has already consumed the fill. A fault
                # between the two settlement sides must roll back every SQL
                # fact and restore the book from the last committed database
                # state, otherwise a ghost fill remains in memory.
                await session.rollback()
                await self._rebuild_engine_book_from_db(
                    session, market_id, reason="spot_settlement_failed"
                )
            raise

        order.notional = total_notional
        order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
        order.avg_price = (total_notional / Decimal(order.filled_quantity)) if Decimal(order.filled_quantity) > ZERO else None
        order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
        order.updated_at = now

        if Decimal(order.filled_quantity) == ZERO:
            if result.placed_on_book:
                order.status = ORDER_STATUS_NEW
            else:
                order.status = ORDER_STATUS_CANCELED if payload.type != ORDER_TYPE_LIMIT or payload.tif not in {TIF_GTC, TIF_POST_ONLY} else ORDER_STATUS_NEW
        elif Decimal(order.remaining_quantity) == ZERO:
            order.status = ORDER_STATUS_FILLED
        elif result.placed_on_book:
            order.status = ORDER_STATUS_PARTIALLY_FILLED
        else:
            order.status = ORDER_STATUS_CANCELED
            order.canceled_at = now

        await self._release_order_leftover(session, market, order, reserve, now)
        if commit:
            await self._flush_commit_or_rebuild_engine(session, market, reason="spot_place_commit_failed")
        else:
            await session.flush()
        return updated_orders, impacted_users, trade_payloads, total_notional

    async def place_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        payload,
        plan: dict,
    ) -> dict:
        """Write-behind replay of a fast-path spot order (no engine mutation)."""
        now = datetime.fromisoformat(plan["now"])
        order = await self._create_order_record(
            session,
            user=user,
            market=market,
            order_id=plan["order_id"],
            payload=payload,
            status=ORDER_STATUS_NEW,
            created_at=now,
            reference_price=_opt_decimal(plan.get("reference_price")),
            max_price=_opt_decimal(plan.get("max_price")),
            min_price=_opt_decimal(plan.get("min_price")),
            reject_reason=None,
        )
        order.sequence_number = int(plan["sequence_number"])
        result = engine_result_from_plan(plan["engine_result"])
        reserve = ReservePlan(
            asset=str(plan["reserve"]["asset"]),
            amount=Decimal(str(plan["reserve"]["amount"])),
        )
        if reserve.amount > ZERO:
            await self._ensure_robot_quote_replay_capacity(
                session,
                user.id,
                market,
                payload.client_order_id,
                reserve.asset,
                reserve.amount,
                now,
                related_order_id=order.order_id,
                user_role=user.role,
            )
            await self.runtime.account_service.reserve(
                session,
                user.id,
                reserve.asset,
                reserve.amount,
                related_order_id=order.order_id,
                note=f"{payload.type}:{payload.side}",
                created_at=now,
            )
        await self._finalize_placed_order(
            session=session,
            market=market,
            user=user,
            order=order,
            payload=payload,
            result=result,
            reserve=reserve,
            sequence_number=order.sequence_number,
            now=now,
            commit=False,
            ingest=False,
        )
        return {"order_id": order.order_id, "status": order.status}

    async def place_limit_gtc_order_batch(self, session: AsyncSession, user: User, payloads: list) -> dict | None:
        if not payloads:
            return None
        symbols = {str(payload.symbol).upper() for payload in payloads}
        if len(symbols) != 1:
            return None
        if any(payload.type != ORDER_TYPE_LIMIT or payload.tif not in {TIF_GTC, TIF_POST_ONLY} for payload in payloads):
            return None
        client_ids = [payload.client_order_id for payload in payloads]
        if any(not client_id or not str(client_id).startswith(BOT_ORDER_PREFIXES) for client_id in client_ids):
            return None
        if len(set(client_ids)) != len(client_ids):
            return None

        symbol = symbols.pop()
        market = await self._get_market(session, symbol)
        if market.product_type != PRODUCT_TYPE_SPOT:
            return None

        started = time.perf_counter()
        now = datetime.now(tz=UTC)
        for payload in payloads:
            payload.symbol = symbol
            self._normalize_payload(market, payload)

        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self._spot_financial_keys(session, market, user.id)
        ):
            await self._reconcile_engine_book(session, market)
            existing_rows = await session.execute(
                select(Order)
                .where(
                    Order.user_id == user.id,
                    Order.market_id == market.id,
                    Order.client_order_id.in_(client_ids),
                    Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                    Order.remaining_quantity > ZERO,
                )
                .order_by(Order.created_at.desc(), Order.id.desc())
            )
            existing_by_client: dict[str, Order] = {}
            for order in existing_rows.scalars():
                if order.client_order_id and order.client_order_id not in existing_by_client:
                    existing_by_client[order.client_order_id] = order

            new_payloads = [payload for payload in payloads if payload.client_order_id not in existing_by_client]
            if not new_payloads:
                self._update_metric(user, "place_order_batch_ms", started)
                return {
                    "ok": True,
                    "fast_path": "resting_limit_gtc",
                    "requested_count": len(payloads),
                    "accepted_count": len(payloads),
                    "failed_count": 0,
                    "items": [
                        {
                            "index": index,
                            "order": await self.serialize_order(session, existing_by_client[payload.client_order_id], market.symbol),
                            "idempotent": True,
                        }
                        for index, payload in enumerate(payloads)
                    ],
                    "failed": [],
                }

            if not market.is_active:
                return None
            price_tick = self._price_tick(market)
            qty_step = self._qty_step(market)
            min_qty = self._min_qty(market)
            buy_prices: list[Decimal] = []
            sell_prices: list[Decimal] = []
            reserves_by_asset: dict[str, Decimal] = defaultdict(Decimal)
            reserve_by_client: dict[str, ReservePlan] = {}
            for payload in new_payloads:
                quantity = self._normalize_qty(market, payload.quantity)
                price = self._normalize_price(market, payload.price)
                if price is None or quantity <= ZERO or price <= ZERO:
                    return None
                if not is_step_aligned(quantity, qty_step) or not is_step_aligned(price, price_tick):
                    return None
                if quantity < min_qty or (price * quantity < Decimal(market.min_notional) and not await min_notional_exempt(session, user, market, payload)):
                    return None
                if payload.side == SIDE_BUY:
                    buy_prices.append(price)
                elif payload.side == SIDE_SELL:
                    sell_prices.append(price)
                else:
                    return None
                reserve = self._build_reserve_plan(market, payload, None, None, None)
                reserve_by_client[payload.client_order_id] = reserve
                reserves_by_asset[reserve.asset] += reserve.amount

            book, _, _ = self.runtime.orderbook_snapshot_unlocked(market.symbol, depth=1)
            best_bid = Decimal(book["bids"][0][0]) if book.get("bids") else None
            best_ask = Decimal(book["asks"][0][0]) if book.get("asks") else None
            if best_ask is not None and any(price >= best_ask for price in buy_prices):
                return None
            if best_bid is not None and any(price <= best_bid for price in sell_prices):
                return None
            if buy_prices and sell_prices and max(buy_prices) >= min(sell_prices):
                return None

            live_statuses = [ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]
            market_limit = settings.max_open_orders_per_user_market
            if market_limit > 0:
                market_count = await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.user_id == user.id, Order.market_id == market.id, Order.status.in_(live_statuses))
                )
                if int(market_count or 0) + len(new_payloads) > market_limit:
                    return None
            total_limit = settings.max_open_orders_per_user_total
            if total_limit > 0:
                total_count = await session.scalar(
                    select(func.count()).select_from(Order).where(Order.user_id == user.id, Order.status.in_(live_statuses))
                )
                if int(total_count or 0) + len(new_payloads) > total_limit:
                    return None

            for asset, amount in reserves_by_asset.items():
                await self.runtime.account_service.ensure_available(session, user.id, asset, amount)

            changed_bids: list[list[str]] = []
            changed_asks: list[list[str]] = []
            new_orders_by_client: dict[str, Order] = {}
            for payload in new_payloads:
                order_id = next_order_id()
                reserve = reserve_by_client[payload.client_order_id]
                if reserve.amount > ZERO:
                    await self.runtime.account_service.reserve(
                        session,
                        user.id,
                        reserve.asset,
                        reserve.amount,
                        related_order_id=order_id,
                        note=f"{payload.type}:{payload.side}",
                        created_at=now,
                    )
                order = await self._create_order_record(
                    session,
                    user=user,
                    market=market,
                    order_id=order_id,
                    payload=payload,
                    status=ORDER_STATUS_NEW,
                    created_at=now,
                    reference_price=None,
                    max_price=None,
                    min_price=None,
                    reject_reason=None,
                    flush=False,
                )
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    NewOrderCommand(
                        order_id=order.order_id,
                        user_id=user.id,
                        side=payload.side,
                        quantity=Decimal(order.quantity),
                        created_at=now,
                        limit_price=Decimal(order.price),
                        can_rest=True,
                        max_price=None,
                        min_price=None,
                        **self._stp_context(user),
                    )
                )
                result = sequencer_result.engine_result
                if result is None or result.fills or not result.placed_on_book:
                    raise OrderValidationError("fast batch only supports non-crossing resting gtc orders")
                order.sequence_number = sequencer_result.sequence_number
                order.notional = ZERO
                order.filled_quantity = ZERO
                order.avg_price = None
                order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
                order.updated_at = now
                order.status = ORDER_STATUS_NEW
                changed_bids.extend(result.changed_bids)
                changed_asks.extend(result.changed_asks)
                new_orders_by_client[payload.client_order_id] = order

            await self._flush_commit_or_rebuild_engine(session, market, reason="spot_batch_place_commit_failed")

        ordered_items: list[dict] = []
        orders_for_broadcast: list[Order] = []
        for index, payload in enumerate(payloads):
            existing = existing_by_client.get(payload.client_order_id)
            if existing is not None:
                ordered_items.append(
                    {
                        "index": index,
                        "order": await self.serialize_order(session, existing, market.symbol),
                        "idempotent": True,
                    }
                )
                continue
            order = new_orders_by_client[payload.client_order_id]
            orders_for_broadcast.append(order)
            ordered_items.append(
                {
                    "index": index,
                    "order": await self.serialize_order(session, order, market.symbol),
                    "trades": [],
                }
            )

        if orders_for_broadcast:
            await self._broadcast_order_flow(session, market.symbol, orders_for_broadcast, {user.id}, changed_bids, changed_asks, [])
        self._update_metric(user, "place_order_batch_ms", started)
        return {
            "ok": True,
            "fast_path": "resting_limit_gtc",
            "requested_count": len(payloads),
            "accepted_count": len(ordered_items),
            "failed_count": 0,
            "items": ordered_items,
            "failed": [],
        }

    async def cancel_order(self, session: AsyncSession, user: User, order_id: str, *, admin_override: bool = False) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        record = await self._load_order_market(session, order_id)
        if record is None:
            raise OrderValidationError("order not found")
        order, market = record

        started = time.perf_counter()
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self._spot_financial_keys(session, market, order.user_id)
        ):
            locked = await self._load_order_market(session, order_id, for_update=True)
            if locked is None:
                raise OrderValidationError("order not found")
            order, market = locked
            if not admin_override and order.user_id != user.id:
                raise OrderValidationError("cannot cancel others order")
            if market.product_type != PRODUCT_TYPE_SPOT:
                raise OrderValidationError("use contract order API for PERP markets")
            if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                raise OrderValidationError("order is not cancelable")
            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
            sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                CancelOrderCommand(order_id=order.order_id)
            )
            side_name = sequencer_result.cancelled_side
            if side_name is None:
                raise OrderValidationError("order not found on book")
            changes = sequencer_result.changed_bids if side_name == SIDE_BUY else sequencer_result.changed_asks
            now = datetime.now(tz=UTC)
            await self._finalize_canceled_order(
                session=session,
                market=market,
                order=order,
                side_name=side_name,
                changes=changes,
                now=now,
                commit=True,
            )

        changed_bids = changes if side_name == SIDE_BUY else []
        changed_asks = changes if side_name == SIDE_SELL else []
        await self._broadcast_order_flow(session, market.symbol, [order], {order.user_id}, changed_bids, changed_asks, [])
        self._fast_orders.pop(order.order_id, None)
        self._fast_unregister_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        self._update_metric(user, "cancel_order_ms", started)
        return {"order": await self.serialize_order(session, order, market.symbol)}

    async def _finalize_canceled_order(
        self,
        *,
        session: AsyncSession,
        market: Market,
        order: Order,
        side_name: str,
        changes: list[list[str]],
        now: datetime,
        commit: bool,
    ) -> None:
        await self._release_remaining_for_cancel(session, market, order, now)
        order.status = ORDER_STATUS_CANCELED
        order.canceled_at = now
        order.updated_at = now
        order.version = int(order.version or 0) + 1
        if commit:
            await self._commit_or_rebuild_engine(session, market, reason="spot_cancel_commit_failed")
        else:
            await session.flush()

    async def cancel_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order_id: str,
        plan: dict,
    ) -> dict:
        locked = await self._load_order_market(session, order_id, for_update=True)
        if locked is None:
            raise OrderValidationError("order not found")
        order, order_market = locked
        if order_market.symbol != market.symbol:
            raise OrderValidationError("order market mismatch")
        if order.user_id != user.id:
            raise OrderValidationError("cannot cancel others order")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            return {"order_id": order.order_id, "status": order.status}
        side_name = str(plan["side"])
        changes = plan.get("changes") or []
        now = datetime.fromisoformat(plan["now"])
        await self._finalize_canceled_order(
            session=session,
            market=order_market,
            order=order,
            side_name=side_name,
            changes=changes,
            now=now,
            commit=False,
        )
        return {"order_id": order.order_id, "status": order.status}

    async def amend_order(self, session: AsyncSession, user: User, order_id: str, payload, *, admin_override: bool = False) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        record = await self._load_order_market(session, order_id)
        if record is None:
            raise OrderValidationError("order not found")
        order, market = record

        started = time.perf_counter()
        now = datetime.now(tz=UTC)
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self._spot_financial_keys(session, market, order.user_id)
        ):
            locked = await self._load_order_market(session, order_id, for_update=True)
            if locked is None:
                raise OrderValidationError("order not found")
            order, market = locked
            if not admin_override and order.user_id != user.id:
                raise OrderValidationError("cannot amend others order")
            if market.product_type != PRODUCT_TYPE_SPOT:
                raise OrderValidationError("use contract order API for PERP markets")
            if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
                raise OrderValidationError("only gtc limit orders are amendable")
            if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                raise OrderValidationError("order is not amendable")
            new_price = self._normalize_price(market, payload.price if payload.price is not None else order.price)
            new_quantity = self._normalize_qty(market, payload.quantity)
            filled_quantity = self._normalize_qty(market, order.filled_quantity)
            current_quantity = self._normalize_qty(market, order.quantity)
            current_price = self._normalize_price(market, order.price)
            current_remaining = self._normalize_qty(market, order.remaining_quantity)

            if current_price is None or new_price is None:
                raise OrderValidationError("limit order price missing")

            new_remaining = self._normalize_qty(market, new_quantity - filled_quantity)
            crossing_book = await self._validate_amend_request(
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

            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
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

            if crossing_book:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    CrossingAmendCommand(
                        order_id=order.order_id,
                        user_id=user.id,
                        side=order.side,
                        new_price=new_price,
                        new_remaining=new_remaining,
                        created_at=now,
                        **self._stp_context(user),
                    )
                )
                side_name = sequencer_result.cancelled_side
                if side_name is None:
                    raise OrderValidationError("order not found on book")
                result = sequencer_result.engine_result
                if result is None:
                    raise OrderValidationError("sequencer did not return engine result")
                order.price = new_price
                order.quantity = new_quantity
                order.remaining_quantity = new_remaining
                order.sequence_number = sequencer_result.sequence_number
                order.updated_at = now
                updated_orders, impacted_users, trade_payloads, _ = await self._apply_engine_fills(
                    session=session,
                    market=market,
                    taker_user=user,
                    taker_order=order,
                    taker_side=order.side,
                    result=result,
                    executed_at=now,
                )
                order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
                if Decimal(order.filled_quantity) == ZERO:
                    order.status = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
                elif Decimal(order.remaining_quantity) == ZERO:
                    order.status = ORDER_STATUS_FILLED
                else:
                    order.status = ORDER_STATUS_PARTIALLY_FILLED
                order.updated_at = now
                order.version = int(order.version or 0) + 1
                await self._commit_or_rebuild_engine(session, market, reason="spot_crossing_amend_commit_failed")
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
                updated_orders[order.order_id] = order
            else:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    AmendOrderCommand(
                        order_id=order.order_id,
                        new_price=new_price,
                        new_remaining=new_remaining,
                    )
                )
                amend = sequencer_result.amend_result
                if amend is None:
                    raise OrderValidationError("order not found on book")

                order.price = new_price
                order.quantity = new_quantity
                order.remaining_quantity = new_remaining
                order.sequence_number = int(amend.sequence_number or order.sequence_number or 0)
                order.updated_at = now
                order.status = ORDER_STATUS_PARTIALLY_FILLED if filled_quantity > ZERO else ORDER_STATUS_NEW
                order.version = int(order.version or 0) + 1
                await self._commit_or_rebuild_engine(session, market, reason="spot_amend_commit_failed")
                updated_orders = {order.order_id: order}
                impacted_users = {order.user_id}
                trade_payloads = []
                changed_bids = amend.changed_bids
                changed_asks = amend.changed_asks

        await self._broadcast_order_flow(
            session,
            market.symbol,
            list(updated_orders.values()),
            impacted_users,
            changed_bids,
            changed_asks,
            trade_payloads,
        )
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            snap = self._fast_order_snapshot(order, market)
            self._fast_orders[order.order_id] = snap
            self._fast_register_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        else:
            self._fast_orders.pop(order.order_id, None)
            self._fast_unregister_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        self._update_metric(user, "amend_order_ms", started)
        return {
            "order": await self.serialize_order(session, order, market.symbol),
            "kept_priority": False if crossing_book else amend.kept_priority,
            "trades": trade_payloads,
        }

    async def amend_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order_id: str,
        payload,
        plan: dict,
    ) -> dict:
        """Write-behind replay of a fast-path spot amend (no engine mutation)."""
        locked = await self._load_order_market(session, order_id, for_update=True)
        if locked is None:
            raise OrderValidationError("order not found")
        order, order_market = locked
        if order_market.symbol != market.symbol:
            raise OrderValidationError("order market mismatch")
        if order.user_id != user.id:
            raise OrderValidationError("cannot amend others order")
        if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
            raise OrderValidationError("only gtc limit orders are amendable")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            return {"order_id": order.order_id, "status": order.status}
        now = datetime.fromisoformat(plan["now"])
        new_price = self._normalize_price(order_market, payload.price if payload.price is not None else order.price)
        new_quantity = self._normalize_qty(order_market, payload.quantity)
        new_remaining = self._normalize_qty(order_market, plan["new_remaining"])
        old_price = self._normalize_price(order_market, order.price)
        old_remaining = self._normalize_qty(order_market, order.remaining_quantity)
        await self._adjust_resting_reserve(
            session,
            order_market,
            order,
            old_price=old_price,
            old_remaining=old_remaining,
            new_price=new_price,
            new_remaining=new_remaining,
            now=now,
        )
        order.price = new_price
        order.quantity = new_quantity
        order.remaining_quantity = new_remaining
        order.sequence_number = int(plan["sequence_number"])
        order.updated_at = now
        order.version = int(order.version or 0) + 1
        if plan.get("amend_kind") == "cross":
            result = engine_result_from_plan(plan["engine_result"])
            await self._apply_engine_fills(
                session=session,
                market=order_market,
                taker_user=user,
                taker_order=order,
                taker_side=order.side,
                result=result,
                executed_at=now,
                ingest=False,
            )
            order.remaining_quantity = self._normalize_qty(order_market, result.remaining_quantity)
            if Decimal(order.filled_quantity) == ZERO:
                order.status = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
            elif Decimal(order.remaining_quantity) == ZERO:
                order.status = ORDER_STATUS_FILLED
            else:
                order.status = ORDER_STATUS_PARTIALLY_FILLED
        else:
            order.status = ORDER_STATUS_PARTIALLY_FILLED if Decimal(order.filled_quantity) > ZERO else ORDER_STATUS_NEW
        await session.flush()
        return {"order_id": order.order_id, "status": order.status}

    async def amend_order_batch(self, session: AsyncSession, user: User, payload) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        order_ids = [item.order_id for item in payload.orders]
        if len(set(order_ids)) != len(order_ids):
            raise OrderValidationError("duplicate order_id in batch")
        symbol = str(payload.symbol).upper()
        started = time.perf_counter()
        now = datetime.now(tz=UTC)
        batch_market = await self._get_market(session, symbol)
        async with self.runtime.market_financial_guard(
            symbol, lambda: self._spot_financial_keys(session, batch_market, user.id)
        ):
            rows = await session.execute(
                select(Order, Market)
                .join(Market, Market.id == Order.market_id)
                .where(Order.order_id.in_(order_ids))
                .with_for_update().execution_options(populate_existing=True)
            )
            loaded = rows.all()
            if len(loaded) != len(order_ids):
                found = {order.order_id for order, _market in loaded}
                missing = [order_id for order_id in order_ids if order_id not in found]
                raise OrderValidationError(f"orders not found: {', '.join(missing)}")
            orders_by_id = {order.order_id: (order, market) for order, market in loaded}
            bot_batch = all(
                bool(order.client_order_id) and str(order.client_order_id).startswith(BOT_ORDER_PREFIXES)
                for order, _market in loaded
            )

            prepared: list[dict] = []
            failed: list[dict] = []
            batch_items: list[BatchAmendItem] = []
            market: Market | None = None
            for index, item in enumerate(payload.orders):
                order, item_market = orders_by_id[item.order_id]
                market = item_market if market is None else market
                if item_market.symbol != symbol:
                    raise OrderValidationError("all orders must belong to payload symbol")
                if item_market.product_type != PRODUCT_TYPE_SPOT:
                    raise OrderValidationError("use contract order API for PERP markets")
                if order.user_id != user.id:
                    raise OrderValidationError("cannot amend others order")
                if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
                    raise OrderValidationError("only gtc limit orders are amendable")
                if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                    if bot_batch:
                        failed.append(
                            {
                                "index": index,
                                "order_id": order.order_id,
                                "client_order_id": order.client_order_id,
                                "error": "order is not amendable",
                            }
                        )
                        continue
                    raise OrderValidationError("order is not amendable")

                new_price = self._normalize_price(item_market, item.price if item.price is not None else order.price)
                new_quantity = self._normalize_qty(item_market, item.quantity)
                filled_quantity = self._normalize_qty(item_market, order.filled_quantity)
                current_quantity = self._normalize_qty(item_market, order.quantity)
                current_price = self._normalize_price(item_market, order.price)
                current_remaining = self._normalize_qty(item_market, order.remaining_quantity)
                if current_price is None or new_price is None:
                    raise OrderValidationError("limit order price missing")
                new_remaining = self._normalize_qty(item_market, new_quantity - filled_quantity)
                if bot_batch and new_remaining <= ZERO:
                    failed.append(
                        {
                            "index": index,
                            "order_id": order.order_id,
                            "client_order_id": order.client_order_id,
                            "error": "quantity cannot be below filled quantity",
                        }
                    )
                    continue
                try:
                    crossing_book = await self._validate_amend_request(
                        session,
                        user,
                        item_market,
                        order,
                        new_price=new_price,
                        new_quantity=new_quantity,
                        new_remaining=new_remaining,
                    )
                except OrderValidationError as exc:
                    if bot_batch:
                        failed.append(
                            {
                                "index": index,
                                "order_id": order.order_id,
                                "client_order_id": order.client_order_id,
                                "error": str(exc),
                            }
                        )
                        continue
                    raise
                if crossing_book:
                    if bot_batch:
                        failed.append(
                            {
                                "index": index,
                                "order_id": order.order_id,
                                "client_order_id": order.client_order_id,
                                "error": "batch amend does not support crossing book",
                            }
                        )
                        continue
                    raise OrderValidationError("batch amend does not support crossing book")

                changed = not (new_price == current_price and new_quantity == current_quantity)
                entry = {
                    "index": index,
                    "order": order,
                    "market": item_market,
                    "new_price": new_price,
                    "new_quantity": new_quantity,
                    "new_remaining": new_remaining,
                    "current_price": current_price,
                    "current_remaining": current_remaining,
                    "filled_quantity": filled_quantity,
                    "changed": changed,
                }
                prepared.append(entry)
                if changed:
                    batch_items.append(BatchAmendItem(order_id=order.order_id, new_price=new_price, new_remaining=new_remaining))

            if market is None:
                raise OrderValidationError("orders is required")

            if batch_items:
                await acquire_sqlite_write_admission(session, existing_order_id=batch_items[0].order_id)
                for entry in prepared:
                    if not entry["changed"]:
                        continue
                    await self._adjust_resting_reserve(
                        session,
                        entry["market"],
                        entry["order"],
                        old_price=entry["current_price"],
                        old_remaining=entry["current_remaining"],
                        new_price=entry["new_price"],
                        new_remaining=entry["new_remaining"],
                        now=now,
                    )

                sequencer_result = await self.runtime.get_symbol_sequencer(symbol).submit(BatchAmendCommand(batch_items))
                batch = sequencer_result.batch_amend_result
                if batch is None:
                    raise OrderValidationError("sequencer did not return batch amend result")
                failed_order_ids = [order_id for order_id, result in batch.results.items() if result is None]
                if failed_order_ids:
                    raise OrderValidationError(f"orders not found on book: {', '.join(failed_order_ids)}")

                for entry in prepared:
                    if not entry["changed"]:
                        continue
                    order = entry["order"]
                    amend = batch.results.get(order.order_id)
                    if amend is None:
                        continue
                    order.price = entry["new_price"]
                    order.quantity = entry["new_quantity"]
                    order.remaining_quantity = entry["new_remaining"]
                    order.sequence_number = int(amend.sequence_number or order.sequence_number or 0)
                    order.updated_at = now
                    order.status = ORDER_STATUS_PARTIALLY_FILLED if entry["filled_quantity"] > ZERO else ORDER_STATUS_NEW
                    order.version = int(order.version or 0) + 1
                await self._commit_or_rebuild_engine(session, market, reason="spot_batch_amend_commit_failed")
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
            else:
                changed_bids = []
                changed_asks = []

        if prepared:
            await self._broadcast_order_flow(
                session,
                symbol,
                [entry["order"] for entry in prepared],
                {user.id},
                changed_bids,
                changed_asks,
                [],
            )
        for entry in prepared:
            order = entry["order"]
            if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                snap = self._fast_order_snapshot(order, entry["market"])
                self._fast_orders[order.order_id] = snap
                self._fast_register_client_id(order.user_id, entry["market"].id, order.client_order_id, order.order_id)
            else:
                self._fast_orders.pop(order.order_id, None)
                self._fast_unregister_client_id(order.user_id, entry["market"].id, order.client_order_id, order.order_id)
        self._update_metric(user, "amend_order_batch_ms", started)
        items = [
            {
                "index": entry["index"],
                "order": await self.serialize_order(session, entry["order"], symbol),
                "changed": entry["changed"],
            }
            for entry in prepared
        ]
        return {
            "ok": not failed,
            "requested_count": len(payload.orders),
            "amended_count": sum(1 for entry in prepared if entry["changed"]),
            "failed_count": len(failed),
            "items": items,
            "failed": failed,
        }

    async def cancel_all(
        self,
        session: AsyncSession,
        user: User,
        symbol: str,
        *,
        admin_override: bool = False,
        target_user_id: int | None = None,
    ) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        market = await self._get_market(session, symbol)
        if market.product_type != PRODUCT_TYPE_SPOT:
            raise OrderValidationError("use contract order API for PERP markets")
        lock_user_id = target_user_id if target_user_id is not None else user.id
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self._spot_financial_keys(session, market, lock_user_id)
        ):
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
            if orders:
                await acquire_sqlite_write_admission(session, existing_order_id=orders[0].order_id)
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
            await self._commit_or_rebuild_engine(session, market, reason="spot_cancel_all_commit_failed")

        if orders:
            await self._broadcast_order_flow(session, market.symbol, orders, impacted_users, changed_bids, changed_asks, [])
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
            display_kline_result = await session.execute(
                delete(DisplayKline).where(DisplayKline.market_id == market.id)
            )
            await self._rebuild_balances_from_ledger(session)
            await session.commit()
            self.runtime.engine.clear_market(market.symbol)
            self.runtime.orderbook_snapshot_unlocked(market.symbol, 50)
            self.runtime.record_orderbook_reconcile(market.symbol, reason="spot_wipe_data_clear", db_reloaded=0)

        self.runtime.market_data.recent_trades.pop(market.symbol, None)
        self.runtime.market_data.display_only_recent_trades.pop(market.symbol, None)
        self.runtime.market_data.live_klines.pop(market.symbol, None)
        self.runtime.market_data.display_only_klines.pop(market.symbol, None)
        self.runtime.market_data.latest_stats.pop(market.symbol, None)
        await self._broadcast_public_orderbook_state(market.symbol)
        return {
            "ok": True,
            "symbol": market.symbol,
            "deleted_orders": order_result.rowcount or 0,
            "deleted_trades": trade_result.rowcount or 0,
            "deleted_klines": kline_result.rowcount or 0,
            "deleted_display_klines": display_kline_result.rowcount or 0,
            "deleted_ledger_entries": deleted_ledger,
        }

    async def wipe_market_klines(self, session: AsyncSession, market: Market, *, clear_display_history: bool = False) -> dict:
        async with self.runtime.market_locks[market.symbol]:
            kline_result = await session.execute(delete(Kline).where(Kline.market_id == market.id))
            display_kline_result = await session.execute(
                delete(DisplayKline).where(DisplayKline.market_id == market.id)
            )
            cutoff = None
            if clear_display_history:
                from app.models.paper_exchange import PaperSystemSetting
                cutoff = int(datetime.now(tz=UTC).timestamp() * 1000)
                key = f"display_history_since:{market.id}"
                boundary = await session.scalar(select(PaperSystemSetting).where(PaperSystemSetting.key == key))
                if boundary is None:
                    boundary = PaperSystemSetting(key=key)
                    session.add(boundary)
                boundary.value_json = {"symbol": market.symbol, "since_ms": cutoff}
            await session.commit()

            data = self.runtime.market_data
            if cutoff is not None:
                data.display_history_since_ms[market.symbol] = cutoff
                data.recent_trades.pop(market.symbol, None)
                data.display_only_recent_trades.pop(market.symbol, None)
            data.live_klines.pop(market.symbol, None)
            data.display_only_klines.pop(market.symbol, None)
            data.sampled_klines.pop(market.symbol, None)
            data.sampled_last_price.pop(market.symbol, None)
            data.sampled_last_source.pop(market.symbol, None)
            data.sampled_last_sample_at_ms.pop(market.symbol, None)
            for key in list(data.sampled_kline_persist_state):
                if key[0] == market.symbol:
                    del data.sampled_kline_persist_state[key]
            history = getattr(self.runtime, "history_store", None)
            deleted_sampled = await history.clear_market_klines(market.symbol) if history else 0
        return {
            "ok": True,
            "scope": "display_history" if clear_display_history else "klines",
            "financial_history_preserved": True,
            "symbol": market.symbol,
            "deleted_sampled_klines": deleted_sampled,
            "deleted_klines": kline_result.rowcount or 0,
            "deleted_display_klines": display_kline_result.rowcount or 0,
        }

    async def reset_user(self, session: AsyncSession, user_id: int) -> None:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one()
        markets_result = await session.execute(select(Market).order_by(Market.symbol.asc()))
        markets = list(markets_result.scalars())
        market_ids = [int(market.id) for market in markets]
        now = datetime.now(tz=UTC)
        changed_by_symbol: dict[str, dict] = defaultdict(
            lambda: {"orders": [], "changed_bids": [], "changed_asks": [], "users": set()}
        )

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
                side_name, _, changes = self.runtime.engine.cancel_order(market.symbol, order.order_id)
                bucket = changed_by_symbol[market.symbol]
                bucket["orders"].append(order)
                bucket["users"].add(order.user_id)
                if side_name == SIDE_BUY:
                    bucket["changed_bids"].extend(changes)
                elif side_name == SIDE_SELL:
                    bucket["changed_asks"].extend(changes)
                order.status = ORDER_STATUS_CANCELED
                order.canceled_at = now
                order.updated_at = now

            await self.runtime.account_service.reset_balances(session, user_id, "default")
            try:
                await session.commit()
                for symbol in changed_by_symbol:
                    self.runtime.publish_orderbook_snapshot_unlocked(symbol)
            except Exception:
                await session.rollback()
                for market_id in market_ids:
                    await self._rebuild_engine_book_from_db(session, market_id, reason="reset_user_commit_failed")
                raise

        for symbol, bucket in changed_by_symbol.items():
            await self._broadcast_order_flow(
                session,
                symbol,
                bucket["orders"],
                bucket["users"],
                bucket["changed_bids"],
                bucket["changed_asks"],
                [],
            )
        balances = await self.serialize_balances(session, user_id)
        await self.runtime.ws.broadcast_private(
            user_id,
            "balances",
            {"channel": "balances", "type": "update", "data": balances, "ts": to_millis(datetime.now(tz=UTC))},
        )

    async def adjust_balance(self, session: AsyncSession, user_id: int, asset: str, amount: Decimal, reason: str) -> None:
        now = datetime.now(tz=UTC)
        async with self.runtime.financial_resources([f"spot:{user_id}:{asset.upper()}"]):
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
            "product_type": order.product_type,
            "side": order.side,
            "position_action": order.position_action,
            "reduce_only": order.reduce_only,
            "leverage": decimal_to_str(to_decimal(order.leverage)) if order.leverage is not None else None,
            "type": order.type,
            "tif": order.tif,
            "status": order.status,
            "sequence_number": int(order.sequence_number or 0),
            "version": int(order.version or 0),
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

    async def serialize_trade(self, trade: Trade, symbol: str, market: Market | None = None) -> dict:
        price = self._normalize_price(market, trade.price) if market is not None else to_decimal(trade.price)
        quantity = self._normalize_qty(market, trade.quantity) if market is not None else to_decimal(trade.quantity)
        return {
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "product_type": trade.product_type,
            "price": decimal_to_str(price),
            "quantity": decimal_to_str(quantity),
            "quote_amount": decimal_to_str(quantize_scale(trade.quote_amount, 8)),
            "taker_side": trade.taker_side,
            "taker_position_action": trade.taker_position_action,
            "maker_position_action": trade.maker_position_action,
            "taker_realized_pnl": decimal_to_str(quantize_scale(trade.taker_realized_pnl, 8)),
            "maker_realized_pnl": decimal_to_str(quantize_scale(trade.maker_realized_pnl, 8)),
            "maker_fee": decimal_to_str(quantize_scale(trade.maker_fee, 8)),
            "taker_fee": decimal_to_str(quantize_scale(trade.taker_fee, 8)),
            "maker_fee_asset": trade.fee_asset_maker,
            "taker_fee_asset": trade.fee_asset_taker,
            "source": trade.source,
            "executed_at": to_millis(trade.executed_at),
        }

    async def serialize_account_trade(self, trade: Trade, symbol: str, user_id: int, market: Market | None = None) -> dict:
        price = self._normalize_price(market, trade.price) if market is not None else to_decimal(trade.price)
        quantity = self._normalize_qty(market, trade.quantity) if market is not None else to_decimal(trade.quantity)
        is_taker = trade.taker_user_id == user_id
        side = trade.taker_side if is_taker else (SIDE_SELL if trade.taker_side == SIDE_BUY else SIDE_BUY)
        return {
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "product_type": trade.product_type,
            "side": side,
            "position_action": trade.taker_position_action if is_taker else trade.maker_position_action,
            "price": decimal_to_str(price),
            "quantity": decimal_to_str(quantity),
            "quote_amount": decimal_to_str(quantize_scale(trade.quote_amount, 8)),
            "fee": decimal_to_str(quantize_scale(trade.taker_fee if is_taker else trade.maker_fee, 8)),
            "fee_asset": trade.fee_asset_taker if is_taker else trade.fee_asset_maker,
            "realized_pnl": decimal_to_str(quantize_scale(trade.taker_realized_pnl if is_taker else trade.maker_realized_pnl, 8)),
            "liquidity_role": "taker" if is_taker else "maker",
            "source": trade.source,
            "ts": to_millis(trade.executed_at),
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

    async def _load_order_market(
        self,
        session: AsyncSession,
        order_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[Order, Market] | None:
        stmt = select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
        if for_update:
            stmt = stmt.with_for_update(of=Order)
        result = await session.execute(stmt.execution_options(populate_existing=True))
        return result.first()

    async def _validate_request(self, session: AsyncSession, user: User, market: Market, payload) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        quantity = self._normalize_qty(market, payload.quantity)
        qty_step = self._qty_step(market)
        min_qty = self._min_qty(market)
        price_tick = self._price_tick(market)
        if not market.is_active:
            raise OrderValidationError("market is inactive")
        if market.is_listed:
            # 对外白标市场按 PRD 上币状态机校验；实验市场只校验 is_active。
            paper_status = str(getattr(market, "paper_status", "TRADING"))
            paper_quote = str(getattr(payload, "client_order_id", "") or "").startswith("paperq-")
            if paper_status != "TRADING" and not (paper_status == "PRE_OPEN" and user.role == ROLE_BOT and paper_quote):
                raise OrderValidationError(f"paper market is {market.paper_status}; new spot orders are disabled")
        if market.product_type != PRODUCT_TYPE_SPOT:
            raise OrderValidationError("use contract order API for PERP markets")
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
            if price * quantity < Decimal(market.min_notional) and not await min_notional_exempt(session, user, market, payload):
                raise OrderValidationError("notional below min_notional")
            if getattr(payload, "tif", None) == TIF_POST_ONLY:
                book, _, _ = self.runtime.orderbook_snapshot_unlocked(market.symbol, depth=1)
                best_ask = Decimal(book["asks"][0][0]) if book.get("asks") else None
                best_bid = Decimal(book["bids"][0][0]) if book.get("bids") else None
                if (payload.side == SIDE_BUY and best_ask is not None and price >= best_ask) or (
                    payload.side == SIDE_SELL and best_bid is not None and price <= best_bid
                ):
                    raise OrderValidationError("post_only order would take liquidity")
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

        await self._validate_open_order_limits(session, user, market, payload)
        reserve = self._build_reserve_plan(market, payload, reference_price, max_price, min_price)
        if reserve.amount > ZERO:
            await self.runtime.account_service.ensure_available(session, user.id, reserve.asset, reserve.amount)
        return reference_price, max_price, min_price

    async def _validate_open_order_limits(self, session: AsyncSession, user: User, market: Market, payload) -> None:
        if payload.type != ORDER_TYPE_LIMIT or payload.tif not in {TIF_GTC, TIF_POST_ONLY}:
            return
        live_statuses = [ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]
        market_limit = settings.max_open_orders_per_user_market
        if market_limit > 0:
            market_count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == user.id,
                    Order.market_id == market.id,
                    Order.status.in_(live_statuses),
                )
            )
            if int(market_count or 0) >= market_limit:
                raise OrderValidationError(
                    f"open order limit exceeded for {market.symbol}: limit={market_limit}"
                )
        total_limit = settings.max_open_orders_per_user_total
        if total_limit > 0:
            total_count = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.user_id == user.id,
                    Order.status.in_(live_statuses),
                )
            )
            if int(total_count or 0) >= total_limit:
                raise OrderValidationError(f"open order limit exceeded for account: limit={total_limit}")

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
    ) -> bool:
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
        if new_price * new_remaining < Decimal(market.min_notional) and not await min_notional_exempt(session, user, market, order):
            raise OrderValidationError("notional below min_notional")
        book, _, _ = self.runtime.orderbook_snapshot_unlocked(market.symbol, depth=1)
        best_bid = Decimal(book["bids"][0][0]) if book.get("bids") else None
        best_ask = Decimal(book["asks"][0][0]) if book.get("asks") else None
        crossing_book = False
        if order.side == SIDE_BUY and best_ask is not None and new_price >= best_ask:
            crossing_book = True
        if order.side == SIDE_SELL and best_bid is not None and new_price <= best_bid:
            crossing_book = True

        old_reserve = self._resting_reserve_amount(market, order.side, current_price, current_remaining)
        new_reserve = self._resting_reserve_amount(market, order.side, new_price, new_remaining)
        if new_reserve > old_reserve:
            await self.runtime.account_service.ensure_available(
                session,
                user.id,
                self._resting_reserve_asset(market, order.side),
                new_reserve - old_reserve,
            )
        return crossing_book

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
            await self._ensure_robot_quote_replay_capacity(
                session,
                order.user_id,
                market,
                order.client_order_id,
                asset,
                delta,
                now,
                related_order_id=order.order_id,
            )
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

    async def _ensure_robot_quote_replay_capacity(
        self,
        session: AsyncSession,
        user_id: int,
        market: Market,
        client_order_id: str | None,
        asset: str,
        amount: Decimal,
        now: datetime,
        *,
        related_order_id: str | None = None,
        user_role: str | None = None,
    ) -> None:
        """Top up a Paper robot maker's durable available before a replay reserve.

        策略机器人（LITE/PERP_MM/内置报价）的报价是重启重建的临时态，其
        write-behind 重放被跳过或批量重试时，耐用余额与快速镜像的冻结会
        漂移。机器人是系统账户：重放需要更多可用余额时按缺口补足（无限资金
        语义），避免可用余额耗尽把整个 writer 拖入 HALT；用户订单与 FLOW
        成交订单不享受该补足，仍然 fail-closed。
        """
        if user_role is None:
            role = await session.scalar(select(User.role).where(User.id == user_id))
            user_role = str(role or "") if role is not None else None
        if not is_paper_robot_topup_eligible(user_role=user_role, client_order_id=client_order_id):
            return
        balance = await self.runtime.account_service.get_balance(session, user_id, asset)
        available = Decimal(balance.available or ZERO) if balance is not None else ZERO
        shortfall = amount - available
        if shortfall <= ZERO:
            return
        await self.runtime.account_service.apply_change(
            session,
            user_id,
            asset,
            available_delta=shortfall,
            frozen_delta=ZERO,
            change_type="paper_maker_reserve_align",
            related_order_id=related_order_id,
            note="paper robot quote replay capacity",
            amount=shortfall,
            created_at=now,
        )

    async def align_robot_maker_pre_fill_replay(
        self,
        session: AsyncSession,
        market: Market,
        anchor: dict,
        *,
        fill_quantity: Decimal | None = None,
        now: datetime,
        minimum_sequence: int | None = None,
    ) -> Order:
        """Align restart-ephemeral robot quote state before replaying a real fill.

        Only mutable resting-order fields and their reserve are aligned.  Prior
        fill history must already match the durable row; otherwise replay stops
        fail-closed instead of overwriting financial history.
        """
        order_id = str(anchor.get("order_id") or "")
        order = await session.scalar(
            select(Order).where(Order.order_id == order_id).with_for_update()
        )
        if order is None:
            raise OrderValidationError(f"maker anchor order not persisted: {order_id}")
        immutable_pairs = {
            "user_id": (int(order.user_id), int(anchor.get("user_id") or 0)),
            "market_id": (int(order.market_id), int(anchor.get("market_id") or 0)),
            "product_type": (str(order.product_type), str(anchor.get("product_type") or "")),
            "side": (str(order.side), str(anchor.get("side") or "")),
            "client_order_id": (str(order.client_order_id or ""), str(anchor.get("client_order_id") or "")),
            "type": (str(order.type), str(anchor.get("type") or "")),
            "tif": (str(order.tif), str(anchor.get("tif") or "")),
        }
        mismatched = [name for name, values in immutable_pairs.items() if values[0] != values[1]]
        if mismatched or int(order.market_id) != int(market.id):
            raise OrderValidationError(
                f"maker anchor identity mismatch order_id={order_id} fields={mismatched}"
            )
        if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
            raise OrderValidationError(f"maker anchor is not live limit GTC: {order_id}")

        price = self._normalize_price(market, anchor.get("price"))
        quantity = self._normalize_qty(market, anchor.get("quantity"))
        filled = self._normalize_qty(market, anchor.get("filled_quantity"))
        remaining = self._normalize_qty(market, anchor.get("remaining_quantity"))
        if price is None or price <= ZERO or remaining <= ZERO:
            raise OrderValidationError(f"invalid maker pre-fill anchor quantities: {order_id}")
        # 快速镜像保存的是全精度数量（策略提交量可能低于 qty_step 精度），
        # 逐字段取整后 filled+remaining 可能与 quantity 相差半个 tick 的
        # 表示尘埃。只容忍量化尘埃，真实的数量漂移（超过 2 个 qty_step）
        # 仍然 fail-closed；随后按归一化后的 filled+remaining 重写总量，
        # 保证耐用行内部恒等式成立。
        dust_tolerance = max(Decimal(str(market.qty_step)) * Decimal("2"), Decimal("1e-12"))
        if abs(quantity - (filled + remaining)) > dust_tolerance:
            raise OrderValidationError(f"invalid maker pre-fill anchor quantities: {order_id}")
        quantity = filled + remaining
        # 机器人报价是重启重建的临时态，快速镜像才是权威状态。耐用行可能
        # 因撤单重放、写后丢弃、批量重试等落后或超前（例如 durable 已被
        # cancel 而镜像仍持有该档）。这里把耐用行直接治愈为 anchor 状态，
        # 而不是 fail-closed HALT；身份字段（user/market/side/client/type）
        # 不一致仍严格拒绝。
        anchor_notional = quantize_scale(anchor.get("notional") or ZERO, 8)
        anchor_avg = to_decimal(anchor["avg_price"]) if anchor.get("avg_price") is not None else None
        anchor_sequence = int(anchor.get("sequence_number") or 0)
        anchor_version = int(anchor.get("version") or 0)
        durable_sequence = int(order.sequence_number or 0)
        durable_version = int(order.version or 0)
        # Order version is the lifecycle clock; sequence_number is price-time
        # priority and is not a second version clock. Older builds could copy a
        # stale QuoteSet command sequence into the fast mirror. If a proven
        # later lifecycle anchor carries a lower priority, put the recovered
        # resting remainder at the back of the market queue. This is
        # conservative (it cannot gain FIFO priority) and avoids persisting an
        # unprovable historical priority after restart.
        aligned_sequence = anchor_sequence
        if anchor_sequence < durable_sequence:
            market_sequence = await session.scalar(
                select(func.max(Order.sequence_number)).where(Order.market_id == market.id)
            )
            aligned_sequence = max(
                anchor_sequence,
                durable_sequence,
                int(market_sequence or 0),
                int(minimum_sequence or 0),
            ) + 1

        old_price = self._normalize_price(market, order.price)
        if old_price is None:
            raise OrderValidationError(f"durable maker limit price missing: {order_id}")
        # Paper maker quote rows are restart-ephemeral.  Bootstrap may have
        # intentionally reset their durable frozen amount to zero while the
        # engine still carries the rebuilt quote.  Before applying the real
        # maker fill, reserve only the quantity consumed by this fill so the
        # durable settlement can safely decrement frozen; leave the unfilled
        # quote remainder ephemeral.  Ordinary durable bot orders keep the
        # existing full reserve alignment path.
        fill_qty = self._normalize_qty(market, fill_quantity or ZERO)
        reserve_asset = self._resting_reserve_asset(market, order.side)
        fill_reserve = self._resting_reserve_amount(market, order.side, price, fill_qty)
        balance = await self.runtime.account_service.get_balance(session, order.user_id, reserve_asset)
        if str(order.client_order_id or "").startswith("paperq-") and fill_qty > ZERO and Decimal(balance.frozen) < fill_reserve - Decimal("1e-12"):
            await self.runtime.account_service.reserve(
                session,
                order.user_id,
                reserve_asset,
                fill_reserve,
                related_order_id=order.order_id,
                note="paper_maker_pre_fill_rehydrate",
                created_at=now,
            )
        else:
            # 机器人报价耐用行可能被撤单重放释放过冻结（或重放漂移），
            # 直接按 anchor 的目标冻结做精确对齐，避免 delta 语义把
            # 已经释放的部分再次释放成负数。
            await self._align_robot_quote_reserve_exact(
                session,
                market,
                order,
                price,
                remaining,
                now,
            )
        order.price = price
        order.quantity = quantity
        order.remaining_quantity = remaining
        order.filled_quantity = filled
        order.notional = Decimal(anchor.get("notional") or ZERO)
        order.avg_price = anchor_avg
        order.sequence_number = aligned_sequence
        self.runtime.observe_sequence(market.symbol, aligned_sequence)
        order.version = anchor_version
        order.status = ORDER_STATUS_PARTIALLY_FILLED if filled > ZERO else ORDER_STATUS_NEW
        order.updated_at = now
        order.canceled_at = None
        await session.flush()
        return order

    async def _align_robot_quote_reserve_exact(
        self,
        session: AsyncSession,
        market: Market,
        order: Order,
        price: Decimal,
        remaining: Decimal,
        now: datetime,
    ) -> None:
        asset = self._resting_reserve_asset(market, order.side)
        required = self._resting_reserve_amount(market, order.side, price, remaining)
        balance = await self.runtime.account_service.get_balance(session, order.user_id, asset)
        frozen = Decimal(balance.frozen or ZERO) if balance is not None else ZERO
        delta = required - frozen
        if delta > ZERO:
            await self.runtime.account_service.apply_change(
                session,
                order.user_id,
                asset,
                available_delta=ZERO,
                frozen_delta=delta,
                change_type="paper_maker_reserve_align",
                related_order_id=order.order_id,
                note="paper robot quote reserve exact align",
                amount=ZERO,
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
        sequence_number: int = 0,
        flush: bool = True,
    ) -> Order:
        order = Order(
            order_id=order_id,
            client_order_id=payload.client_order_id,
            user_id=user.id,
            account_run_id=user.current_account_run_id,
            market_id=market.id,
            product_type=market.product_type,
            side=payload.side,
            type=payload.type,
            tif=payload.tif,
            status=status,
            sequence_number=sequence_number,
            version=0,
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
        if flush:
            await session.flush()
        return order

    async def _load_orders_map(self, session: AsyncSession, order_ids: set[str]) -> dict[str, Order]:
        if not order_ids:
            return {}
        rows = await session.execute(select(Order).where(Order.order_id.in_(order_ids)))
        return {order.order_id: order for order in rows.scalars()}

    async def _load_user_roles(self, session: AsyncSession, user_ids: set[int]) -> dict[int, str]:
        if not user_ids:
            return {}
        rows = await session.execute(select(User.id, User.role).where(User.id.in_(user_ids)))
        return {user_id: role for user_id, role in rows.all()}

    @staticmethod
    def _classify_trade_source(
        *,
        taker_user: User,
        taker_order: Order,
        maker_order: Order,
        maker_user_role: str | None,
    ) -> str:
        client_order_ids = [
            value
            for value in (taker_order.client_order_id, maker_order.client_order_id)
            if value
        ]
        if any(value.startswith(FLOW_ORDER_PREFIXES) or "-flow-" in value for value in client_order_ids):
            return "flow"
        if any(value.startswith(BOT_ORDER_PREFIXES) for value in client_order_ids):
            return "bot"
        if any(value.startswith(SEED_ORDER_PREFIXES) for value in client_order_ids):
            return BOOTSTRAP_SEED_SOURCE
        if taker_user.role == ROLE_BOT or maker_user_role == ROLE_BOT:
            return "bot"
        return "user"

    async def _apply_engine_fills(
        self,
        *,
        session: AsyncSession,
        market: Market,
        taker_user: User,
        taker_order: Order,
        taker_side: str,
        result,
        executed_at: datetime,
        ingest: bool = True,
    ) -> tuple[dict[str, Order], set[int], list[dict], Decimal]:
        maker_order_ids = {fill.maker_order_id for fill in result.fills}
        maker_orders = await self._load_orders_map(session, maker_order_ids)
        # The taker transaction already owns SQLite's writer lock. Waiting for
        # another connection to materialize a missing maker would deadlock it.
        # Missing financial facts remain an explicit consistency failure.
        if len(maker_orders) < len(maker_order_ids):
            missing = sorted(maker_order_ids - set(maker_orders))
            raise OrderValidationError(f"maker orders not persisted: {missing}")
        maker_user_roles = await self._load_user_roles(session, {order.user_id for order in maker_orders.values()})
        impacted_users = {taker_user.id}
        updated_orders: dict[str, Order] = {taker_order.order_id: taker_order}
        trade_payloads: list[dict] = []
        total_notional = ZERO

        taker_fee_rate = await self._get_fee_rate(session, taker_user.id, market.id, taker=True, market=market)
        maker_fee_rates: dict[int, Decimal] = {}

        for fill in result.fills:
            maker_order = maker_orders[fill.maker_order_id]
            source = self._classify_trade_source(
                taker_user=taker_user,
                taker_order=taker_order,
                maker_order=maker_order,
                maker_user_role=maker_user_roles.get(maker_order.user_id),
            )
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
                taker_user=taker_user,
                taker_order=taker_order,
                maker_order=maker_order,
                quantity=fill.quantity,
                price=fill.price,
                taker_fee_rate=taker_fee_rate,
                maker_fee_rate=maker_fee_rates[maker_order.user_id],
                executed_at=executed_at,
                source=source,
                ingest=ingest,
            )
            total_notional += Decimal(trade.quote_amount)
            impacted_users.add(maker_order.user_id)
            updated_orders[maker_order.order_id] = maker_order
            if ingest:
                self.runtime.market_data.ingest_trade(
                    market.symbol,
                    price=Decimal(trade.price),
                    quantity=Decimal(trade.quantity),
                    side=taker_side,
                    ts=executed_at,
                    trade_id=trade.trade_id,
                    price_scale=market.price_precision,
                    qty_scale=market.qty_precision,
                    source=trade.source,
                )
            trade_payloads.append(await self.serialize_trade(trade, market.symbol, market=market))
            await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "1m")
            await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "5m")

        return updated_orders, impacted_users, trade_payloads, total_notional

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
        source: str,
        ingest: bool = True,
    ) -> Trade:
        quote_amount = quantity * price
        maker_user = await session.scalar(select(User).where(User.id == maker_order.user_id))
        maker_fee, maker_fee_asset = self._fee_amount(maker_order.side, maker_fee_rate, quantity, quote_amount, market)
        taker_fee, taker_fee_asset = self._fee_amount(taker_order.side, taker_fee_rate, quantity, quote_amount, market)

        trade = Trade(
            trade_id=next_trade_id(),
            business_key=(
                f"SPOT:{market.id}:{taker_order.order_id}:{int(taker_order.version or 0)}:"
                f"{maker_order.order_id}:{int(maker_order.version or 0)}"
            ),
            market_id=market.id,
            price=price,
            quantity=quantity,
            quote_amount=quote_amount,
            taker_order_id=taker_order.order_id,
            maker_order_id=maker_order.order_id,
            product_type=market.product_type,
            taker_position_action=taker_order.position_action,
            maker_position_action=maker_order.position_action,
            taker_realized_pnl=ZERO,
            maker_realized_pnl=ZERO,
            taker_user_id=taker_user.id,
            maker_user_id=maker_order.user_id,
            account_run_id=taker_user.current_account_run_id,
            taker_account_run_id=taker_user.current_account_run_id,
            maker_account_run_id=getattr(maker_user, "current_account_run_id", None),
            global_run_id=getattr(self.runtime, "paper_global_run_id", None),
            taker_side=taker_order.side,
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            fee_asset_maker=maker_fee_asset,
            fee_asset_taker=taker_fee_asset,
            source=source,
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
            current.version = int(current.version or 0) + 1
            if current.remaining_quantity <= ZERO:
                current.remaining_quantity = ZERO
                current.status = ORDER_STATUS_FILLED
            else:
                current.status = ORDER_STATUS_PARTIALLY_FILLED

        await session.flush()
        await enqueue_financial_outbox(
            session,
            event_type="trade_committed",
            account_domain="spot",
            aggregate_type="trade",
            aggregate_id=trade.trade_id,
            idempotency_key=f"outbox:trade:{trade.business_key}",
            occurred_at=trade.executed_at,
            source_event_id=trade.trade_id,
            payload={
                "trade_id": trade.trade_id,
                "business_key": trade.business_key,
                "market_id": trade.market_id,
                "product_type": trade.product_type,
                "taker_order_id": trade.taker_order_id,
                "maker_order_id": trade.maker_order_id,
                "taker_user_id": trade.taker_user_id,
                "maker_user_id": trade.maker_user_id,
                "price": str(trade.price),
                "quantity": str(trade.quantity),
                "quote_amount": str(trade.quote_amount),
            },
        )
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
        is_paper_quote = str(order.client_order_id or "").startswith("paperq-")
        if not is_paper_quote and platform_durable_contract():
            # 策略机器人（LITE/PERP_MM）报价的 durable 冻结可能因重放漂移
            # 缺失；与内置 paperq 报价同语义按成交所需补足，保证成交结算的
            # 余额恒等式成立（maker 系统账户语义，不改变用户事实）。
            role = await session.scalar(select(User.role).where(User.id == user_id))
            is_paper_quote = (
                str(role or "") == ROLE_BOT and not is_flow_client_order_id(order.client_order_id)
            )
        if is_paper_quote:
            # Paper 报价的 write-behind 重放被跳过时，DB 里 maker 的冻结可能
            # 缺失。settle 前按成交所需补足冻结，保证用户成交结算 maker 侧时
            # 余额恒等式成立（maker 是系统账户，补足不改变任何用户事实）。
            if order.side == SIDE_BUY:
                reserve_price = Decimal(order.price) if order.type == ORDER_TYPE_LIMIT and order.price is not None else price
                required = reserve_price * quantity
                asset = market.quote_asset
            else:
                required = quantity
                asset = market.base_asset
            balance = await session.scalar(
                select(Balance).where(Balance.user_id == user_id, Balance.asset == asset)
            )
            frozen = Decimal(balance.frozen or ZERO) if balance is not None else ZERO
            # Ignore only representation dust in the synthetic maker reserve
            # alignment, never an actual trade/fee delta. SQLite can reload a
            # step-aligned reserve 1e-16 below the required Decimal value.
            if required - frozen > Decimal("1e-12"):
                await account.apply_change(
                    session,
                    user_id,
                    asset,
                    available_delta=ZERO,
                    frozen_delta=required - frozen,
                    change_type="paper_maker_reserve_align",
                    related_order_id=order.order_id,
                    related_trade_id=trade_id,
                    note="paper robot quote settle align",
                    amount=ZERO,
                    created_at=executed_at,
                )
            if order.side == SIDE_BUY and order.type == ORDER_TYPE_LIMIT and order.price is not None:
                # 成交价高于挂单价（滑点）时 refund 为负，可用余额需补足。
                refund = reserve_price * quantity - quote_amount
                if refund < ZERO:
                    quote_balance = await session.scalar(
                        select(Balance).where(Balance.user_id == user_id, Balance.asset == market.quote_asset)
                    )
                    available = Decimal(quote_balance.available or ZERO) if quote_balance is not None else ZERO
                    needed = -refund - available
                    if needed > ZERO:
                        await account.apply_change(
                            session,
                            user_id,
                            market.quote_asset,
                            available_delta=needed,
                            frozen_delta=ZERO,
                            change_type="paper_maker_reserve_align",
                            related_order_id=order.order_id,
                            related_trade_id=trade_id,
                            note="paper robot quote settle spend",
                            amount=needed,
                            created_at=executed_at,
                        )
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
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED} and order.type == ORDER_TYPE_LIMIT and order.tif in {TIF_GTC, TIF_POST_ONLY}:
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
        await self._release_for_robot_quote(
            session,
            order,
            asset,
            release,
            now,
            note="cancel_release",
        )

    async def _release_for_robot_quote(
        self,
        session: AsyncSession,
        order: Order,
        asset: str,
        amount: Decimal,
        now: datetime,
        *,
        note: str,
    ) -> None:
        """Release a quote reserve, clamped to the actual durable frozen amount.

        机器人报价的 durable 冻结可能因 write-behind 重放漂移低于行内剩余量
        （报价 amend 被丢弃/合并后取消重放）。释放量钳到实际冻结即可：报价
        冻结本来就是重启重建的临时态，多退部分本就没有冻结可退。用户订单与
        FLOW 成交订单不经过该路径，保持严格 fail-closed。
        """
        if platform_durable_contract() and amount > ZERO:
            role = await session.scalar(select(User.role).where(User.id == order.user_id))
            if str(role or "") == ROLE_BOT and not is_flow_client_order_id(order.client_order_id):
                balance = await self.runtime.account_service.get_balance(session, order.user_id, asset)
                frozen = Decimal(balance.frozen or ZERO) if balance is not None else ZERO
                if amount > frozen:
                    amount = frozen
        if amount <= ZERO:
            return
        await self.runtime.account_service.release(
            session,
            order.user_id,
            asset,
            amount,
            related_order_id=order.order_id,
            note=note,
            created_at=now,
        )

    async def _broadcast_public_orderbook_state(self, symbol: str) -> None:
        snapshot, seq, updated_at_ms = await self.runtime.orderbook_snapshot(symbol, 100)
        await self.runtime.ws.broadcast_public_orderbook(
            symbol,
            {
                "channel": "orderbook",
                "type": "snapshot",
                "symbol": symbol,
                "seq": seq,
                "bids": snapshot["bids"],
                "asks": snapshot["asks"],
                "ts": updated_at_ms,
            },
        )
        stats = self.runtime.market_data.compute_stats(symbol, snapshot)
        await self.runtime.ws.broadcast_public(
            "stats",
            symbol,
            {"channel": "stats", "type": "update", "symbol": symbol, "data": stats},
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
        # Public WS subscribers can watch up to 100 levels. Broadcasting a full
        # snapshot here avoids shallow-book delta drift on the frontend, where
        # removed top levels otherwise cannot promote deeper levels into view.
        snapshot, seq, updated_at_ms = await self.runtime.orderbook_snapshot(
            symbol,
            100,
            advance=bool(changed_bids or changed_asks),
        )
        if changed_bids or changed_asks:
            await self.runtime.ws.broadcast_public_orderbook(
                symbol,
                {
                    "channel": "orderbook",
                    "type": "snapshot",
                    "symbol": symbol,
                    "seq": seq,
                    "bids": snapshot["bids"],
                    "asks": snapshot["asks"],
                    "ts": updated_at_ms,
                },
            )
        stats = self.runtime.market_data.compute_stats(symbol, snapshot)
        await self.runtime.ws.broadcast_public(
            "stats",
            symbol,
            {"channel": "stats", "type": "update", "symbol": symbol, "data": stats},
        )
        user_roles = await self._load_user_roles(session, impacted_users)
        non_bot_users = {user_id for user_id in impacted_users if user_roles.get(user_id) != ROLE_BOT}
        if trade_payloads:
            await self.runtime.ws.broadcast_public(
                "trades",
                symbol,
                {"channel": "trades", "type": "update", "symbol": symbol, "items": trade_payloads},
            )
            for interval in ["1s", "5s", "15s", "1m", "5m", "15m"]:
                items = self.runtime.market_data.get_public_klines(symbol, interval, 1)
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
        private_trades_by_user: dict[int, list[dict]] = {}
        if trade_payloads and non_bot_users:
            trade_ids = [item["trade_id"] for item in trade_payloads]
            trade_rows = await session.execute(
                select(Trade, Market)
                .join(Market, Market.id == Trade.market_id)
                .where(Trade.trade_id.in_(trade_ids))
                .order_by(Trade.executed_at.desc())
            )
            for trade, market in trade_rows.all():
                for user_id in (trade.taker_user_id, trade.maker_user_id):
                    if user_id not in non_bot_users:
                        continue
                    private_trades_by_user.setdefault(user_id, []).append(
                        await self.serialize_account_trade(trade, market.symbol, user_id, market=market)
                    )
        market_symbol = symbol
        current_ms = to_millis(datetime.now(tz=UTC))
        for user_id in impacted_users:
            is_bot = user_roles.get(user_id) == ROLE_BOT
            relevant_orders = [relevant for relevant in orders if relevant.user_id == user_id]
            flow_ioc_only = (
                is_bot
                and bool(relevant_orders)
                and all(str(relevant.client_order_id or "").startswith("flowv2-") for relevant in relevant_orders)
            )
            if flow_ioc_only:
                continue
            should_push_balances = True
            if is_bot:
                last_balance_ms = self._last_bot_balance_broadcast_ms[user_id]
                should_push_balances = current_ms - last_balance_ms >= BOT_BALANCE_BROADCAST_MIN_INTERVAL_MS
            if should_push_balances:
                balances = await self.serialize_balances(session, user_id)
                await self.runtime.ws.broadcast_private(
                    user_id,
                    "balances",
                    {"channel": "balances", "type": "update", "data": balances, "ts": current_ms},
                )
                if is_bot:
                    self._last_bot_balance_broadcast_ms[user_id] = current_ms
            for relevant in relevant_orders:
                await self.runtime.ws.broadcast_private(
                    user_id,
                    "orders",
                    {"channel": "orders", "type": "update", "data": await self.serialize_order(session, relevant, market_symbol)},
                )
            if private_trades_by_user.get(user_id):
                await self.runtime.ws.broadcast_private(
                    user_id,
                    "trades",
                    {
                        "channel": "trades",
                        "type": "update",
                        "items": private_trades_by_user[user_id],
                        "ts": to_millis(datetime.now(tz=UTC)),
                    },
                )
            should_push_ledger = not is_bot
            if should_push_ledger:
                ledger_items = await self.serialize_ledger_entries(session, user_id, None, 20)
                await self.runtime.ws.broadcast_private(
                    user_id,
                    "ledger",
                    {"channel": "ledger", "type": "update", "items": ledger_items, "ts": current_ms},
                )

    def _update_metric(self, user: User, metric: str, started: float) -> None:
        if user.role != ROLE_BOT:
            return
        self.runtime.bot_metrics[user.username][metric] = round((time.perf_counter() - started) * 1000, 2)
        self.runtime.bot_metrics[user.username]["last_heartbeat"] = to_millis(datetime.now(tz=UTC))

    # ------------------------------------------------------------------
    # Fast path: in-memory clearinghouse + write-behind persistence
    # ------------------------------------------------------------------
    async def load_fast_path_state(self, session: AsyncSession) -> None:
        rows = await session.execute(select(FeeProfile))
        for row in rows.scalars():
            self._fee_rate_cache[(int(row.user_id), int(row.market_id), True)] = Decimal(row.taker_fee_rate)
            self._fee_rate_cache[(int(row.user_id), int(row.market_id), False)] = Decimal(row.maker_fee_rate)
        query = (
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(
                Order.product_type == PRODUCT_TYPE_SPOT,
                Order.type == ORDER_TYPE_LIMIT,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
            )
        )
        if settings.persistence_mode == "memory":
            query = query.join(User, User.id == Order.user_id).where(User.role != "mm_bot")
        rows = await session.execute(query)
        for order, market in rows.all():
            snap = self._fast_order_snapshot(order, market)
            self._fast_orders[order.order_id] = snap
            if order.client_order_id:
                self._fast_client_ids[(int(order.user_id), int(market.id), str(order.client_order_id))] = order.order_id

    def _fast_register_client_id(self, user_id: int, market_id: int, client_order_id: str | None, order_id: str) -> None:
        if client_order_id:
            self._fast_client_ids[(int(user_id), int(market_id), str(client_order_id))] = order_id

    def _fast_unregister_client_id(self, user_id: int, market_id: int, client_order_id: str | None, order_id: str) -> None:
        if not client_order_id:
            return
        key = (int(user_id), int(market_id), str(client_order_id))
        if self._fast_client_ids.get(key) == order_id:
            self._fast_client_ids.pop(key, None)

    def fast_state_metrics_snapshot(self) -> dict[str, int]:
        return dict(self._fast_state_metrics)

    @staticmethod
    def _is_live_fast_order(snap: dict) -> bool:
        return (
            str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
            and Decimal(str(snap.get("remaining_quantity") or ZERO)) > ZERO
        )

    @staticmethod
    def _fast_spot_reservation(snap: dict, fallback_market: Market | None = None) -> tuple[str, Decimal] | None:
        remaining = Decimal(str(snap.get("remaining_quantity") or ZERO))
        if remaining <= ZERO:
            return None
        same_fallback_market = (
            fallback_market is not None
            and int(snap.get("market_id") or -1) == int(fallback_market.id)
        )
        if str(snap.get("side") or "") == SIDE_BUY:
            asset = str(snap.get("quote_asset") or (fallback_market.quote_asset if same_fallback_market else "")).upper()
            price = Decimal(str(snap.get("price") or ZERO))
            amount = price * remaining
        elif str(snap.get("side") or "") == SIDE_SELL:
            asset = str(snap.get("base_asset") or (fallback_market.base_asset if same_fallback_market else "")).upper()
            amount = remaining
        else:
            return None
        if not asset or amount <= ZERO:
            return None
        return asset, amount

    def _reconcile_ephemeral_spot_ghosts_locked(
        self,
        *,
        user: User,
        market: Market,
    ) -> dict:
        """Evict proven engine-absent sandbox bot quotes and release only excess reserve.

        The caller holds ``clearinghouse.global_lock``.  Engine presence is the
        proof boundary: strict/customer mirrors are never touched, and a
        blocked writer is never interpreted as an engine miss.  Releasing only
        frozen balance above all engine-backed mirror reservations also makes
        the recovery safe when the missing order was already financially
        settled before its mirror became stale.
        """
        result = {
            "checked": False,
            "evicted_count": 0,
            "released": {},
            "release_shortfall_count": 0,
            "order_ids": [],
        }
        if (
            str(user.role) != ROLE_BOT
            or not (
                settings.persistence_mode == "memory"
                or platform_durable_contract()
            )
            or not self._fast_writer_ready()
        ):
            return result
        result["checked"] = True
        live_for_user = [
            (order_id, snap)
            for order_id, snap in self._fast_orders.items()
            if int(snap.get("user_id") or -1) == int(user.id)
            and self._is_live_fast_order(snap)
        ]
        candidates = [
            (order_id, snap)
            for order_id, snap in live_for_user
            if int(snap.get("market_id") or -1) == int(market.id)
            and str(snap.get("symbol") or "").upper() == market.symbol.upper()
            and order_id not in getattr(self.runtime.engine.books.get(market.symbol), "orders", {})
        ]
        if not candidates:
            return result

        protected_by_asset: defaultdict[str, Decimal] = defaultdict(Decimal)
        unknown_engine_backed_reservation = False
        for order_id, snap in live_for_user:
            symbol = str(snap.get("symbol") or "").upper()
            book = self.runtime.engine.books.get(symbol)
            if book is None or order_id not in book.orders:
                continue
            reservation = self._fast_spot_reservation(snap, market)
            if reservation is None:
                unknown_engine_backed_reservation = True
                continue
            asset, amount = reservation
            protected_by_asset[asset] += amount

        stale_by_asset: defaultdict[str, Decimal] = defaultdict(Decimal)
        for _order_id, snap in candidates:
            reservation = self._fast_spot_reservation(snap, market)
            if reservation is not None:
                asset, amount = reservation
                stale_by_asset[asset] += amount

        released: dict[str, str] = {}
        shortfalls = 0
        for asset, stale_amount in stale_by_asset.items():
            state = self.runtime.clearinghouse.spot_snapshot(int(user.id), asset)
            releasable = ZERO
            if state is not None and not unknown_engine_backed_reservation:
                excess_frozen = max(ZERO, Decimal(state.frozen) - protected_by_asset[asset])
                releasable = min(stale_amount, excess_frozen)
            if releasable > ZERO:
                self.runtime.clearinghouse.release_spot(int(user.id), asset, releasable)
                released[asset] = decimal_to_str(releasable)
                self._fast_state_metrics["ephemeral_ghost_release_events"] += 1
            if releasable < stale_amount:
                shortfalls += 1
                self._fast_state_metrics["ephemeral_ghost_release_shortfalls"] += 1

        evicted_ids: list[str] = []
        for order_id, expected_snap in candidates:
            if self._fast_orders.get(order_id) is not expected_snap:
                continue
            self._fast_unregister_client_id(
                int(expected_snap["user_id"]),
                int(market.id),
                expected_snap.get("client_order_id"),
                order_id,
            )
            self._fast_orders.pop(order_id, None)
            evicted_ids.append(order_id)
        self._fast_state_metrics["ephemeral_ghost_reconciliations"] += 1
        self._fast_state_metrics["ephemeral_ghost_evictions"] += len(evicted_ids)
        result.update(
            evicted_count=len(evicted_ids),
            released=released,
            release_shortfall_count=shortfalls,
            order_ids=evicted_ids,
        )
        logging.getLogger("order_service").warning(
            "spot ephemeral ghost recovery symbol=%s user_id=%s evicted=%s released=%s shortfalls=%s",
            market.symbol,
            user.id,
            len(evicted_ids),
            released,
            shortfalls,
        )
        return result

    async def reconcile_ephemeral_spot_ghosts(self, user: User, market: Market) -> dict:
        async with self.runtime.clearinghouse.global_lock:
            return self._reconcile_ephemeral_spot_ghosts_locked(user=user, market=market)

    @staticmethod
    def _fast_order_snapshot(order: Order, market: Market) -> dict:
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "user_id": int(order.user_id),
            "market_id": int(market.id),
            "symbol": market.symbol,
            "base_asset": market.base_asset,
            "quote_asset": market.quote_asset,
            "product_type": order.product_type,
            "side": order.side,
            "position_action": order.position_action,
            "reduce_only": bool(order.reduce_only),
            "leverage": decimal_to_str(to_decimal(order.leverage)) if order.leverage is not None else None,
            "type": order.type,
            "tif": order.tif,
            "status": order.status,
            "sequence_number": int(order.sequence_number or 0),
            "version": int(order.version or 0),
            "price": decimal_to_str(to_decimal(order.price)) if order.price is not None else None,
            "quantity": decimal_to_str(to_decimal(order.quantity)),
            "filled_quantity": decimal_to_str(to_decimal(order.filled_quantity)),
            "remaining_quantity": decimal_to_str(to_decimal(order.remaining_quantity)),
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

    def _fast_serialize_order(self, market: Market, snap: dict) -> dict:
        return {
            "order_id": snap["order_id"],
            "client_order_id": snap["client_order_id"],
            "symbol": market.symbol,
            "product_type": snap["product_type"],
            "side": snap["side"],
            "position_action": snap["position_action"],
            "reduce_only": snap["reduce_only"],
            "leverage": snap["leverage"],
            "type": snap["type"],
            "tif": snap["tif"],
            "status": snap["status"],
            "sequence_number": int(snap["sequence_number"] or 0),
            "version": int(snap["version"] or 0),
            "price": snap["price"],
            "quantity": snap["quantity"],
            "filled_quantity": snap["filled_quantity"],
            "remaining_quantity": snap["remaining_quantity"],
            "avg_price": snap["avg_price"],
            "notional": snap["notional"],
            "reference_price": snap["reference_price"],
            "protection_bps": snap["protection_bps"],
            "max_price": snap["max_price"],
            "min_price": snap["min_price"],
            "reject_reason": snap["reject_reason"],
            "created_at": snap["created_at"],
            "updated_at": snap["updated_at"],
        }

    async def fast_open_order_items(
        self,
        session: AsyncSession,
        user_id: int,
        *,
        symbol: str | None = None,
    ) -> list[dict]:
        """Return the authoritative live SPOT mirror for an ephemeral robot.

        In memory mode a robot's simple quote amendments are intentionally not
        materialized.  Reading those orders back from SQLite would therefore
        rewind the maker to an older price/quantity on every reconciliation.
        Callers must still enforce the ``memory + mm_bot`` policy boundary.
        """

        normalized_symbol = str(symbol or "").upper()
        async with self.runtime.clearinghouse.global_lock:
            snapshots = [
                dict(snap)
                for snap in self._fast_orders.values()
                if int(snap.get("user_id") or -1) == int(user_id)
                and str(snap.get("product_type") or "") == PRODUCT_TYPE_SPOT
                and str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                and Decimal(snap.get("remaining_quantity") or 0) > ZERO
                and (not normalized_symbol or str(snap.get("symbol") or "").upper() == normalized_symbol)
            ]
        market_ids = {
            int(snap.get("market_id") or 0)
            for snap in snapshots
            if int(snap.get("market_id") or 0) > 0
        }
        markets: dict[int, Market] = {}
        if market_ids:
            rows = await session.execute(select(Market).where(Market.id.in_(market_ids)))
            markets = {int(market.id): market for market in rows.scalars()}
        items = [
            self._fast_serialize_order(markets[int(snap["market_id"])], snap)
            for snap in snapshots
            if int(snap.get("market_id") or 0) in markets
        ]
        items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
        return items

    async def fast_orderbook_invariant(self, symbol: str) -> dict:
        """Compare SPOT engine nodes with the live fast mirror by content.

        ID/count equality alone misses the failure mode where SQLite and the
        engine contain the same order IDs but robot prices/remaining quantities
        have diverged.  The fast mirror is the correct comparison peer for
        ephemeral robot quotes.
        """

        normalized_symbol = str(symbol).upper()
        async with self.runtime.clearinghouse.global_lock:
            mirror = {
                order_id: dict(snap)
                for order_id, snap in self._fast_orders.items()
                if str(snap.get("symbol") or "").upper() == normalized_symbol
                and str(snap.get("product_type") or "") == PRODUCT_TYPE_SPOT
                and str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                and Decimal(snap.get("remaining_quantity") or 0) > ZERO
            }
            book = self.runtime.engine.books.get(normalized_symbol)
            all_engine = dict(book.orders) if book is not None else {}
            writer = getattr(self.runtime, "persistence_writer", None)
            robot_user_ids = {
                int(value)
                for value in getattr(writer, "_robot_user_ids", set())
            }
            robot_user_ids.update(int(snap.get("user_id") or 0) for snap in mirror.values())
            engine = {
                order_id: node
                for order_id, node in all_engine.items()
                if int(node.user_id) in robot_user_ids
            }

        engine_only = sorted(set(engine) - set(mirror))
        mirror_only = sorted(set(mirror) - set(engine))
        mismatches: list[dict] = []
        for order_id in sorted(set(engine) & set(mirror)):
            node = engine[order_id]
            snap = mirror[order_id]
            expected_values = {
                "user_id": int(snap.get("user_id") or 0),
                "side": str(snap.get("side") or ""),
                "price": Decimal(str(snap.get("price") or 0)),
                "remaining_quantity": Decimal(str(snap.get("remaining_quantity") or 0)),
                "sequence_number": int(snap.get("sequence_number") or 0),
            }
            actual_values = {
                "user_id": int(node.user_id),
                "side": str(node.side),
                "price": Decimal(node.price),
                "remaining_quantity": Decimal(node.remaining),
                "sequence_number": int(node.sequence_number or 0),
            }
            differing = [
                key for key in expected_values if expected_values[key] != actual_values[key]
            ]
            if differing:
                expected = {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in expected_values.items()
                }
                actual = {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in actual_values.items()
                }
                mismatches.append(
                    {
                        "order_id": order_id,
                        "fields": differing,
                        "fast_mirror": expected,
                        "engine": actual,
                    }
                )
        return {
            "fast_mirror_open_order_count": len(mirror),
            "engine_open_order_count": len(engine),
            "engine_only_count": len(engine_only),
            "fast_mirror_only_count": len(mirror_only),
            "content_mismatch_count": len(mismatches),
            "engine_only_sample": engine_only[:20],
            "fast_mirror_only_sample": mirror_only[:20],
            "content_mismatch_sample": mismatches[:20],
            "recovery_metrics": self.fast_state_metrics_snapshot(),
        }

    async def _fast_fee_rate(self, session: AsyncSession, market: Market, user_id: int, *, taker: bool) -> Decimal:
        key = (int(user_id), int(market.id), taker)
        cached = self._fee_rate_cache.get(key)
        if cached is not None:
            return cached
        rate = await self._get_fee_rate(session, user_id, market.id, taker=taker, market=market)
        self._fee_rate_cache[key] = rate
        return rate

    @staticmethod
    def _fast_engine_plan(result: EngineResult) -> dict:
        return {
            "remaining_quantity": decimal_to_str(Decimal(result.remaining_quantity)),
            "placed_on_book": bool(result.placed_on_book),
            "stop_reason": result.stop_reason,
            "stp_action": result.stp_action,
            "stp_reason": result.stp_reason,
            "stp_intercept_count": result.stp_intercept_count,
            "stp_decremented_quantity": decimal_to_str(result.stp_decremented_quantity),
            "fills": [
                {
                    "maker_order_id": fill.maker_order_id,
                    "maker_user_id": fill.maker_user_id,
                    "price": decimal_to_str(fill.price),
                    "quantity": decimal_to_str(fill.quantity),
                }
                for fill in result.fills
            ],
            "changed_bids": [[str(px), str(qty)] for px, qty in result.changed_bids],
            "changed_asks": [[str(px), str(qty)] for px, qty in result.changed_asks],
        }

    @staticmethod
    def _fast_maker_pre_fill_anchor(snap: dict) -> dict:
        """Return the durable replay checkpoint immediately before a maker fill."""
        fields = (
            "order_id",
            "client_order_id",
            "user_id",
            "market_id",
            "product_type",
            "side",
            "position_action",
            "reduce_only",
            "type",
            "tif",
            "status",
            "sequence_number",
            "version",
            "price",
            "quantity",
            "filled_quantity",
            "remaining_quantity",
            "avg_price",
            "notional",
            "leverage",
        )
        return {field: snap.get(field) for field in fields}

    async def _fast_apply_fill_settlement(
        self,
        *,
        session: AsyncSession,
        market: Market,
        taker_snap: dict,
        result: EngineResult,
        executed_at: datetime,
    ) -> tuple[list[dict], list[dict], set[int], Decimal, list[dict]]:
        deltas: list[dict] = []
        trade_payloads: list[dict] = []
        impacted: set[int] = {int(taker_snap["user_id"])}
        total_notional = ZERO
        maker_pre_fill_anchors: dict[str, dict] = {}
        taker_fee_rate = await self._fast_fee_rate(session, market, int(taker_snap["user_id"]), taker=True)
        maker_fee_rates: dict[int, Decimal] = {}
        capture_durable_maker_anchors = True
        for fill in result.fills:
            maker = self._fast_orders.get(fill.maker_order_id)
            if maker is None:
                raise OrderValidationError(f"fast maker order not found: {fill.maker_order_id}")
            maker_user_id = int(maker["user_id"])
            if capture_durable_maker_anchors and str(fill.maker_order_id) not in maker_pre_fill_anchors:
                anchor = self._fast_maker_pre_fill_anchor(maker)
                anchor["user_role"] = await session.scalar(
                    select(User.role).where(User.id == maker_user_id)
                )
                maker_pre_fill_anchors[str(fill.maker_order_id)] = anchor
            impacted.add(maker_user_id)
            if maker_user_id not in maker_fee_rates:
                maker_fee_rates[maker_user_id] = await self._fast_fee_rate(session, market, maker_user_id, taker=False)
            quote_amount = Decimal(fill.price) * Decimal(fill.quantity)
            total_notional += quote_amount
            maker_fee, maker_fee_asset = self._fee_amount(maker["side"], maker_fee_rates[maker_user_id], Decimal(fill.quantity), quote_amount, market)
            taker_fee, taker_fee_asset = self._fee_amount(taker_snap["side"], taker_fee_rate, Decimal(fill.quantity), quote_amount, market)
            trade_id = next_trade_id()
            source = self._fast_trade_source(taker_snap, maker)
            deltas.extend(
                self._fast_side_settlement_deltas(
                    snap=maker,
                    market=market,
                    quantity=Decimal(fill.quantity),
                    price=Decimal(fill.price),
                    fee=maker_fee,
                    fee_asset=maker_fee_asset,
                    trade_id=trade_id,
                )
            )
            deltas.extend(
                self._fast_side_settlement_deltas(
                    snap=taker_snap,
                    market=market,
                    quantity=Decimal(fill.quantity),
                    price=Decimal(fill.price),
                    fee=taker_fee,
                    fee_asset=taker_fee_asset,
                    trade_id=trade_id,
                )
            )
            for current in (taker_snap, maker):
                filled = Decimal(current["filled_quantity"]) + Decimal(fill.quantity)
                remaining = max(ZERO, Decimal(current["remaining_quantity"]) - Decimal(fill.quantity))
                notional = Decimal(current["notional"] or ZERO) + quote_amount
                current["filled_quantity"] = decimal_to_str(filled)
                current["remaining_quantity"] = decimal_to_str(remaining)
                current["notional"] = decimal_to_str(notional)
                current["avg_price"] = decimal_to_str(notional / filled) if filled > ZERO else None
                current["updated_at"] = to_millis(executed_at)
                current["version"] = int(current["version"] or 0) + 1
                current["status"] = ORDER_STATUS_FILLED if remaining <= ZERO else ORDER_STATUS_PARTIALLY_FILLED
            trade_payloads.append(
                {
                    "trade_id": trade_id,
                    "taker_user_id": int(taker_snap["user_id"]),
                    "maker_user_id": int(maker["user_id"]),
                    "symbol": market.symbol,
                    "product_type": market.product_type,
                    "price": decimal_to_str(self._normalize_price(market, fill.price)),
                    "quantity": decimal_to_str(self._normalize_qty(market, fill.quantity)),
                    "quote_amount": decimal_to_str(quantize_scale(quote_amount, 8)),
                    "taker_side": taker_snap["side"],
                    "taker_position_action": taker_snap["position_action"],
                    "maker_position_action": maker["position_action"],
                    "taker_realized_pnl": "0",
                    "maker_realized_pnl": "0",
                    "maker_fee": decimal_to_str(quantize_scale(maker_fee, 8)),
                    "taker_fee": decimal_to_str(quantize_scale(taker_fee, 8)),
                    "maker_fee_asset": maker_fee_asset,
                    "taker_fee_asset": taker_fee_asset,
                    "source": source,
                    "executed_at": to_millis(executed_at),
                }
            )
        return deltas, trade_payloads, impacted, total_notional, list(maker_pre_fill_anchors.values())

    @staticmethod
    def _fast_trade_source(taker_snap: dict, maker_snap: dict) -> str:
        ids = [str(taker_snap.get("client_order_id") or ""), str(maker_snap.get("client_order_id") or "")]
        if any(value.startswith(FLOW_ORDER_PREFIXES) or "-flow-" in value for value in ids):
            return "flow"
        if any(value.startswith(BOT_ORDER_PREFIXES) for value in ids):
            return "bot"
        return "user"

    @staticmethod
    def _fast_side_settlement_deltas(
        *,
        snap: dict,
        market: Market,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        fee_asset: str,
        trade_id: str,
    ) -> list[dict]:
        quote_amount = quantity * price
        deltas: list[dict] = []
        order_id = snap["order_id"]
        user_id = int(snap["user_id"])
        if snap["side"] == SIDE_BUY:
            reserve_price = Decimal(snap["price"]) if snap["type"] == ORDER_TYPE_LIMIT and snap.get("price") else price
            reserve_consumed = reserve_price * quantity
            refund = reserve_consumed - quote_amount
            deltas.append(
                {
                    "user_id": user_id,
                    "asset": market.quote_asset,
                    "available_delta": refund,
                    "frozen_delta": -reserve_consumed,
                    "change_type": "trade_settlement",
                    "related_order_id": order_id,
                    "related_trade_id": trade_id,
                    "note": "quote_spend",
                    "amount": -quote_amount,
                }
            )
            deltas.append(
                {
                    "user_id": user_id,
                    "asset": market.base_asset,
                    "available_delta": quantity,
                    "frozen_delta": ZERO,
                    "change_type": "trade_settlement",
                    "related_order_id": order_id,
                    "related_trade_id": trade_id,
                    "note": "base_receive",
                    "amount": quantity,
                }
            )
        else:
            deltas.append(
                {
                    "user_id": user_id,
                    "asset": market.base_asset,
                    "available_delta": ZERO,
                    "frozen_delta": -quantity,
                    "change_type": "trade_settlement",
                    "related_order_id": order_id,
                    "related_trade_id": trade_id,
                    "note": "base_spend",
                    "amount": -quantity,
                }
            )
            deltas.append(
                {
                    "user_id": user_id,
                    "asset": market.quote_asset,
                    "available_delta": quote_amount,
                    "frozen_delta": ZERO,
                    "change_type": "trade_settlement",
                    "related_order_id": order_id,
                    "related_trade_id": trade_id,
                    "note": "quote_receive",
                    "amount": quote_amount,
                }
            )
        if fee != ZERO:
            deltas.append(
                {
                    "user_id": user_id,
                    "asset": fee_asset,
                    "available_delta": -fee,
                    "frozen_delta": ZERO,
                    "change_type": "fee",
                    "related_order_id": order_id,
                    "related_trade_id": trade_id,
                    "note": "trade_fee",
                    "amount": -fee,
                }
            )
        return deltas

    def _fast_writer_ready(self) -> bool:
        self.runtime.engine.fault.check()
        writer = getattr(self.runtime, "persistence_writer", None)
        if writer is None:
            return False
        return (
            not writer._stopping
            and writer._blocked_task is None
            and getattr(writer, "_critical_blocked", None) is None
            and not writer._queue.full()
        )

    async def _fast_enqueue(self, task: dict) -> None:
        writer = getattr(self.runtime, "persistence_writer", None)
        if writer is None:
            raise OrderValidationError("persistence writer unavailable")
        from app.services.persistence_contract import facts_durable

        if callable(getattr(writer, "is_capturing", None)) and writer.is_capturing():
            # The enclosing QuoteSet owns its single durable execution tail.
            writer.enqueue(task)
            return
        result = task.get("engine_result") or {}
        ephemeral = callable(getattr(writer, "is_ephemeral_quote_task", None)) and writer.is_ephemeral_quote_task(task)
        if ((str(settings.core_mode or "legacy").lower() == "unified" and not ephemeral)
                or (facts_durable() and bool(result.get("fills")))):
            await writer.enqueue_durable(task)
            return
        if writer.enqueue(task):
            return
        # The queue is full (high-frequency burst). Drain it first, then retry;
        # the engine mutation already happened so a task MUST land.
        await writer.flush(timeout=2.0)
        if not writer.enqueue(task):
            raise RuntimeError("persistence queue full after flush")

    async def _fast_enqueue_or_restore(self, session: AsyncSession, market: Market, task: dict) -> None:
        try:
            await self._fast_enqueue(task)
        except Exception:
            if str(settings.core_mode or "legacy").lower() != "unified":
                raise
            await session.rollback()
            await self._rebuild_engine_book_from_db(
                session,
                int(market.id),
                reason="causal_durable_execution_failed",
            )
            await self.runtime.clearinghouse.load_from_db(session)
            self._fast_orders.clear()
            self._fast_client_ids.clear()
            await self.load_fast_path_state(session)
            raise

    async def _fast_broadcast(
        self,
        market: Market,
        *,
        changed_bids: list[list[str]],
        changed_asks: list[list[str]],
        trade_payloads: list[dict],
    ) -> None:
        old_published = self.runtime.published_orderbooks.get(market.symbol)
        snapshot, seq, updated_at_ms = self.runtime.orderbook_snapshot_unlocked(market.symbol, 100)
        if not (changed_bids or changed_asks or trade_payloads):
            return
        # The matcher has already published the immutable book before this
        # method is entered.  Delta JSON construction, statistics and generic
        # WS fan-out are deliberately outside the quote patch critical path;
        # orderbook delivery remains latest-wins in WebSocketManager.
        self._fast_broadcast_scheduler.submit(
            market.symbol,
            {
                "market": market,
                "old_published": old_published,
                "snapshot": snapshot,
                "seq": seq,
                "updated_at_ms": updated_at_ms,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": trade_payloads,
            },
            merge=self._merge_fast_broadcast_payload,
        )
        # Let the deferred task enter the event loop before the caller's
        # await returns.  This preserves the observable "trade/stats publish
        # was scheduled" contract for lightweight WS adapters without pulling
        # their network wait back into the QuotePatch critical path.
        await asyncio.sleep(0)

    async def _deferred_fast_broadcast(
        self,
        market: Market,
        *,
        old_published,
        snapshot: dict[str, list[list[str]]],
        seq: int,
        updated_at_ms: int,
        changed_bids: list[list[str]],
        changed_asks: list[list[str]],
        trade_payloads: list[dict],
    ) -> None:
        if old_published is None:
            orderbook_payload = self.runtime.orderbook_snapshot_payload(market.symbol, source="fast_snapshot")
            orderbook_changed = bool(snapshot["bids"] or snapshot["asks"])
        else:
            orderbook_payload = self.runtime.orderbook_delta_payload(
                market.symbol,
                old_published,
                source="fast_delta",
            )
            orderbook_changed = bool(orderbook_payload.get("bids") or orderbook_payload.get("asks"))
        if orderbook_changed and (changed_bids or changed_asks):
            self.runtime.ws.broadcast_public_orderbook_nowait(
                market.symbol,
                orderbook_payload,
                immediate=True,
            )
        stats = self.runtime.market_data.compute_stats(market.symbol, snapshot)
        await self.runtime.ws.broadcast_public(
            "stats",
            market.symbol,
            {"channel": "stats", "type": "update", "symbol": market.symbol, "data": stats},
        )
        if trade_payloads:
            await self.runtime.ws.broadcast_public(
                "trades",
                market.symbol,
                {"channel": "trades", "type": "update", "symbol": market.symbol, "items": trade_payloads},
            )
            for interval in ["1s", "5s", "15s", "1m", "5m", "15m"]:
                items = self.runtime.market_data.get_public_klines(market.symbol, interval, 1)
                if items:
                    await self.runtime.ws.broadcast_public(
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

    def _fast_ingest_trade_payloads(
        self,
        market: Market,
        trade_payloads: list[dict],
        *,
        executed_at: datetime,
        taker_side: str,
    ) -> None:
        """Publish completed fast settlement into the live market-data view.

        Write-behind replay uses ``ingest=False``, so each fast trade enters
        recent trades and K-lines exactly once. Every trade reaching this
        settlement helper has a durable financial event. Display-only
        synthetic FLOW never calls this helper or the canonical K-line path.
        """
        for trade in trade_payloads:
            self.runtime.market_data.ingest_trade(
                market.symbol,
                price=Decimal(str(trade["price"])),
                quantity=Decimal(str(trade["quantity"])),
                side=taker_side,
                ts=executed_at,
                trade_id=str(trade["trade_id"]),
                price_scale=market.price_precision,
                qty_scale=market.qty_precision,
                source=str(trade.get("source") or "unknown"),
            )

    async def place_order_fast(
        self,
        session: AsyncSession,
        user: User,
        payload,
        *,
        now: datetime | None = None,
        broadcast: bool = True,
        order_id: str | None = None,
        command_sequence: int | None = None,
        allow_client_order_reuse: bool = False,
        market_override: Market | None = None,
        quote_fast_path: bool = False,
    ) -> dict | None:
        if not self._fast_writer_ready():
            return None
        started = time.perf_counter()
        market = market_override or await self._get_market(session, payload.symbol)
        if broadcast and market.product_type == PRODUCT_TYPE_SPOT:
            try:
                synthetic = await execute_synthetic_flow(
                    session=session,
                    runtime=self.runtime,
                    market=market,
                    user=user,
                    payload=payload,
                    fast_orders=self._fast_orders,
                )
            except SyntheticFlowUnavailableError:
                return None
            except SyntheticFlowValidationError as exc:
                raise OrderValidationError(str(exc)) from exc
            if synthetic is not None:
                is_new = bool(synthetic.pop("_synthetic_new", False))
                if is_new:
                    await broadcast_synthetic_flow(self.runtime, market, synthetic["trades"])
                self._update_metric(user, "place_order_fast_ms", started)
                return synthetic
            if not self._fast_writer_ready():
                return None
        now = now or datetime.now(tz=UTC)
        self._normalize_payload(market, payload)
        order_id = str(order_id or next_order_id())
        if payload.client_order_id and not allow_client_order_reuse:
            client_key = (int(user.id), int(market.id), str(payload.client_order_id))
            existing_order_id = self._fast_client_ids.get(client_key)
            if existing_order_id is not None:
                snap = self._fast_orders.get(existing_order_id)
                if snap is not None and snap["status"] in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                    existing = await self._load_live_client_order(
                        session, user, market, payload.client_order_id
                    )
                    if (existing is not None and existing.order_id == existing_order_id):
                        return {
                            "order": self._fast_serialize_order(market, snap),
                            "idempotent": True,
                            "fast_path": True,
                        }
                    # Stale mirror (e.g. slow-path cancel raced with the writer):
                    # drop it and fall through to a fresh create/DB idempotency.
                    self._fast_orders.pop(existing_order_id, None)
                    self._fast_unregister_client_id(int(user.id), int(market.id), payload.client_order_id, existing_order_id)
            existing = await self._load_live_client_order(
                session, user, market, payload.client_order_id
            )
            if existing is not None:
                snap = self._fast_order_snapshot(existing, market)
                self._fast_orders[existing.order_id] = snap
                self._fast_client_ids[client_key] = existing.order_id
                return {
                    "order": self._fast_serialize_order(market, snap),
                    "idempotent": True,
                    "fast_path": True,
                }
        if quote_fast_path and user.role == ROLE_BOT:
            # QuoteSet's planner has already validated market/tick/notional
            # shape.  Reserve/matcher still enforce the financial boundary;
            # skip one SQL open-order-limit/balance read per level.
            reference_price, max_price, min_price = None, None, None
        else:
            reference_price, max_price, min_price = await self._validate_request(session, user, market, payload)
        reserve = self._build_reserve_plan(market, payload, reference_price, max_price, min_price)
        effective_command_sequence = command_sequence
        if (
            command_sequence is not None
            and int(command_sequence) <= int(self.runtime.sequence_numbers[market.symbol])
        ):
            if (settings.persistence_mode == "memory" and user.role == ROLE_BOT):
                effective_command_sequence = None
            else:
                raise OrderValidationError("stale place priority sequence")
        async with self.runtime.fast_matching_guard(market.symbol):
            if reserve.amount > ZERO:
                try:
                    self.runtime.clearinghouse.reserve_spot(user.id, reserve.asset, reserve.amount)
                except ValueError as exc:
                    raise OrderValidationError(str(exc)) from exc
            try:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    NewOrderCommand(
                        order_id=order_id,
                        user_id=user.id,
                        side=payload.side,
                        quantity=Decimal(payload.quantity),
                        created_at=now,
                        limit_price=Decimal(payload.price) if payload.price is not None else None,
                        can_rest=payload.type == ORDER_TYPE_LIMIT and payload.tif in {TIF_GTC, TIF_POST_ONLY},
                        max_price=max_price,
                        min_price=min_price,
                        sequence_number=effective_command_sequence,
                        **self._stp_context(user),
                    )
                )
            except NotExecuted:
                if reserve.amount > ZERO:
                    self.runtime.clearinghouse.release_spot(user.id, reserve.asset, reserve.amount)
                raise
            result = sequencer_result.engine_result
            if result is None:
                raise OrderValidationError("sequencer did not return engine result")
            taker_snap = {
                "order_id": order_id,
                "client_order_id": payload.client_order_id,
                "user_id": int(user.id),
                "market_id": int(market.id),
                "symbol": market.symbol,
                "base_asset": market.base_asset,
                "quote_asset": market.quote_asset,
                "product_type": market.product_type,
                "side": payload.side,
                "position_action": getattr(payload, "position_action", None),
                "reduce_only": False,
                "leverage": None,
                "type": payload.type,
                "tif": payload.tif,
                "status": ORDER_STATUS_NEW,
                "sequence_number": sequencer_result.sequence_number,
                "version": 0,
                "price": decimal_to_str(to_decimal(payload.price)) if payload.price is not None else None,
                "quantity": decimal_to_str(to_decimal(payload.quantity)),
                "filled_quantity": "0",
                "remaining_quantity": decimal_to_str(to_decimal(payload.quantity)),
                "avg_price": None,
                "notional": "0",
                "reference_price": decimal_to_str(reference_price) if reference_price is not None else None,
                "protection_bps": getattr(payload, "protection_bps", None),
                "max_price": decimal_to_str(max_price) if max_price is not None else None,
                "min_price": decimal_to_str(min_price) if min_price is not None else None,
                "reject_reason": None,
                "created_at": to_millis(now),
                "updated_at": to_millis(now),
            }
            (
                deltas,
                trade_payloads,
                impacted,
                total_notional,
                maker_pre_fill_anchors,
            ) = await self._fast_apply_fill_settlement(
                session=session,
                market=market,
                taker_snap=taker_snap,
                result=result,
                executed_at=now,
            )
            taker_snap["notional"] = decimal_to_str(total_notional)
            taker_snap["filled_quantity"] = decimal_to_str(Decimal(taker_snap["filled_quantity"]))
            taker_snap["remaining_quantity"] = decimal_to_str(result.remaining_quantity)
            stp_terminal = result.stp_action in {"cancel_taker", "reject"} and not result.placed_on_book
            if Decimal(taker_snap["filled_quantity"]) == ZERO:
                taker_snap["status"] = ORDER_STATUS_NEW if result.placed_on_book else (
                    ORDER_STATUS_CANCELED if stp_terminal or payload.type != ORDER_TYPE_LIMIT or payload.tif not in {TIF_GTC, TIF_POST_ONLY} else ORDER_STATUS_NEW
                )
            elif Decimal(taker_snap["remaining_quantity"]) == ZERO:
                taker_snap["status"] = ORDER_STATUS_FILLED
            elif result.placed_on_book:
                taker_snap["status"] = ORDER_STATUS_PARTIALLY_FILLED
            else:
                taker_snap["status"] = ORDER_STATUS_CANCELED
            for delta in deltas:
                self.runtime.clearinghouse.apply_spot_delta(
                    int(delta["user_id"]),
                    str(delta["asset"]),
                    available_delta=Decimal(delta["available_delta"]),
                    frozen_delta=Decimal(delta["frozen_delta"]),
                )
            if result.placed_on_book:
                self._fast_orders[order_id] = taker_snap
                self._fast_register_client_id(int(user.id), int(market.id), payload.client_order_id, order_id)
            elif taker_snap["status"] in {ORDER_STATUS_FILLED, ORDER_STATUS_CANCELED}:
                self._fast_unregister_client_id(int(user.id), int(market.id), payload.client_order_id, order_id)
            task = {
                "kind": "spot_place",
                "symbol": market.symbol,
                "user_id": int(user.id),
                "order_id": order_id,
                "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload),
                "engine_result": self._fast_engine_plan(result),
                "sequence_number": sequencer_result.sequence_number,
                "now": now.isoformat(),
                "reference_price": decimal_to_str(reference_price) if reference_price is not None else None,
                "max_price": decimal_to_str(max_price) if max_price is not None else None,
                "min_price": decimal_to_str(min_price) if min_price is not None else None,
                "reserve": {"asset": reserve.asset, "amount": decimal_to_str(reserve.amount)},
                "allow_client_order_reuse": bool(allow_client_order_reuse),
                "ephemeral_robot_quote": user.role == ROLE_BOT,
                "maker_anchor_version": 1,
                "maker_pre_fill_anchors": maker_pre_fill_anchors,
            }
            await self._fast_enqueue_or_restore(session, market, task)
            self._fast_ingest_trade_payloads(
                market,
                trade_payloads,
                executed_at=now,
                taker_side=payload.side,
            )
        if broadcast:
            await self._fast_broadcast(
                market,
                changed_bids=result.changed_bids,
                changed_asks=result.changed_asks,
                trade_payloads=trade_payloads,
            )
        self._update_metric(user, "place_order_fast_ms", started)
        response = {
            "order": self._fast_serialize_order(market, taker_snap),
            "trades": trade_payloads,
            "fast_path": True,
            "engine_result": self._fast_engine_plan(result),
        }
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": result.changed_bids,
                "changed_asks": result.changed_asks,
                "trade_payloads": trade_payloads,
            }
        return response

    async def cancel_order_fast(
        self,
        session: AsyncSession,
        user: User,
        order_id: str,
        *,
        broadcast: bool = True,
        now: datetime | None = None,
        command_sequence: int | None = None,
    ) -> dict | None:
        snap = self._fast_orders.get(order_id)
        if snap is None or not self._fast_writer_ready():
            return None
        started = time.perf_counter()
        market = await self._get_market(session, snap["symbol"])
        now = now or datetime.now(tz=UTC)
        async with self.runtime.fast_matching_guard(market.symbol):
            sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                CancelOrderCommand(order_id=order_id, sequence_number=command_sequence)
            )
            side_name = sequencer_result.cancelled_side
            if side_name is None:
                self._reconcile_ephemeral_spot_ghosts_locked(user=user, market=market)
                return None
            remaining = Decimal(snap["remaining_quantity"])
            if snap["side"] == SIDE_BUY:
                release = (Decimal(snap["price"]) * remaining) if snap.get("price") else ZERO
                asset = market.quote_asset
            else:
                release = remaining
                asset = market.base_asset
            if release > ZERO:
                self.runtime.clearinghouse.release_spot(int(snap["user_id"]), asset, release)
            changes = sequencer_result.changed_bids if side_name == SIDE_BUY else sequencer_result.changed_asks
            snap["status"] = ORDER_STATUS_CANCELED
            snap["updated_at"] = to_millis(now)
            snap["version"] = int(snap["version"] or 0) + 1
            self._fast_unregister_client_id(int(snap["user_id"]), int(market.id), snap.get("client_order_id"), order_id)
            self._fast_orders.pop(order_id, None)
            task = {
                "kind": "spot_cancel",
                "symbol": market.symbol,
                "user_id": int(user.id),
                "order_id": order_id,
                "side": side_name,
                "changes": [[str(px), str(qty)] for px, qty in changes],
                "now": now.isoformat(),
            }
            await self._fast_enqueue_or_restore(session, market, task)
        changed_bids = changes if side_name == SIDE_BUY else []
        changed_asks = changes if side_name == SIDE_SELL else []
        if broadcast:
            await self._fast_broadcast(market, changed_bids=changed_bids, changed_asks=changed_asks, trade_payloads=[])
        self._update_metric(user, "cancel_order_fast_ms", started)
        response = {"order": self._fast_serialize_order(market, snap), "fast_path": True}
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": [],
            }
        return response

    async def cancel_all_fast(self, session: AsyncSession, user: User, symbol: str) -> dict | None:
        if not self._fast_writer_ready():
            return None
        market = await self._get_market(session, symbol)
        normalized = market.symbol.upper()
        targets = [
            snap
            for snap in self._fast_orders.values()
            if snap["symbol"] == normalized
            and int(snap["user_id"]) == int(user.id)
            and snap["status"] in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
        ]
        count = 0
        flows: list[dict] = []
        for snap in targets:
            result = await self.cancel_order_fast(session, user, snap["order_id"], broadcast=False)
            if result is None:
                return None
            count += 1
            flow = result.get("_fast_flow")
            if flow is not None:
                flows.append(flow)
        if flows:
            await self._fast_broadcast(
                market,
                changed_bids=[item for flow in flows for item in flow["changed_bids"]],
                changed_asks=[item for flow in flows for item in flow["changed_asks"]],
                trade_payloads=[],
            )
        return {"count": count, "fast_path": True}

    async def cancel_batch_fast(
        self,
        session: AsyncSession,
        user: User,
        symbol: str,
        order_ids: list[str],
    ) -> dict | None:
        if not self._fast_writer_ready():
            return None
        market = await self._get_market(session, symbol)
        results: list[dict] = []
        failed: list[dict] = []
        flows: list[dict] = []
        for index, order_id in enumerate(order_ids):
            result = await self.cancel_order_fast(session, user, order_id, broadcast=False)
            if result is None:
                return None
            results.append({"index": index, **result})
            flow = result.get("_fast_flow")
            if flow is not None:
                flows.append(flow)
        if flows:
            await self._fast_broadcast(
                market,
                changed_bids=[item for flow in flows for item in flow["changed_bids"]],
                changed_asks=[item for flow in flows for item in flow["changed_asks"]],
                trade_payloads=[],
            )
        return {
            "ok": not failed,
            "requested_count": len(order_ids),
            "canceled_count": len(results),
            "failed_count": len(failed),
            "items": results,
            "failed": failed,
            "fast_path": True,
        }

    async def amend_order_fast(
        self,
        session: AsyncSession,
        user: User,
        order_id: str,
        payload,
        *,
        broadcast: bool = True,
        now: datetime | None = None,
        command_sequence: int | None = None,
        market_override: Market | None = None,
        bbo_snapshot: dict[str, list[list[str]]] | None = None,
    ) -> dict | None:
        snap = self._fast_orders.get(order_id)
        if snap is None or not self._fast_writer_ready():
            return None
        started = time.perf_counter()
        market = market_override or await self._get_market(session, snap["symbol"])
        now = now or datetime.now(tz=UTC)
        new_price = self._normalize_price(market, payload.price if payload.price is not None else snap["price"])
        new_quantity = self._normalize_qty(market, payload.quantity)
        filled = Decimal(snap["filled_quantity"])
        new_remaining = self._normalize_qty(market, new_quantity - filled)
        old_price = self._normalize_price(market, snap["price"])
        old_remaining = Decimal(snap["remaining_quantity"])
        if old_price is None or new_price is None:
            return None
        book = bbo_snapshot
        if book is None:
            book, _, _ = await self.runtime.orderbook_snapshot(market.symbol, 1)
        best_ask = Decimal(book["asks"][0][0]) if book["asks"] else None
        best_bid = Decimal(book["bids"][0][0]) if book["bids"] else None
        crossing_book = (
            (snap["side"] == SIDE_BUY and best_ask is not None and new_price >= best_ask)
            or (snap["side"] == SIDE_SELL and best_bid is not None and new_price <= best_bid)
        )
        effective_command_sequence = command_sequence
        resets_priority = crossing_book or new_price != old_price or new_remaining > old_remaining
        if (
            resets_priority
            and command_sequence is not None
            and int(command_sequence) <= int(snap.get("sequence_number") or 0)
        ):
            if (settings.persistence_mode == "memory" and user.role == ROLE_BOT):
                # QuoteSet plans are prepared before execution and their
                # explicit operation sequence can be overtaken by intervening
                # fast batch updates.  Let the live symbol sequencer allocate a
                # fresh priority instead of moving price-time priority back.
                effective_command_sequence = None
            else:
                raise OrderValidationError("stale amend priority sequence")
        async with self.runtime.fast_matching_guard(market.symbol):
            if crossing_book:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    CrossingAmendCommand(
                        order_id=order_id,
                        user_id=int(user.id),
                        side=snap["side"],
                        new_price=new_price,
                        new_remaining=new_remaining,
                        created_at=now,
                        sequence_number=effective_command_sequence,
                        **self._stp_context(user),
                    )
                )
                if sequencer_result.cancelled_side is None or sequencer_result.engine_result is None:
                    self._reconcile_ephemeral_spot_ghosts_locked(user=user, market=market)
                    return None
                result = sequencer_result.engine_result
            else:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    AmendOrderCommand(
                        order_id=order_id,
                        new_price=new_price,
                        new_remaining=new_remaining,
                        sequence_number=effective_command_sequence,
                    )
                )
                if sequencer_result.amend_result is None:
                    self._reconcile_ephemeral_spot_ghosts_locked(user=user, market=market)
                    return None
                result = None
            # reserve adjustment
            if snap["side"] == SIDE_BUY:
                old_reserved = old_price * old_remaining
                new_reserved = new_price * new_remaining
                asset = market.quote_asset
            else:
                old_reserved = old_remaining
                new_reserved = new_remaining
                asset = market.base_asset
            diff = new_reserved - old_reserved
            if diff > ZERO:
                self.runtime.clearinghouse.reserve_spot(int(snap["user_id"]), asset, diff)
            elif diff < ZERO:
                self.runtime.clearinghouse.release_spot(int(snap["user_id"]), asset, -diff)
            snap["price"] = decimal_to_str(new_price)
            snap["quantity"] = decimal_to_str(new_quantity)
            snap["remaining_quantity"] = decimal_to_str(new_remaining)
            priority_sequence = (
                int(sequencer_result.sequence_number)
                if crossing_book
                else int(sequencer_result.amend_result.sequence_number)
            )
            snap["sequence_number"] = priority_sequence
            snap["updated_at"] = to_millis(now)
            snap["version"] = int(snap["version"] or 0) + 1
            changed_bids: list[list[str]] = []
            changed_asks: list[list[str]] = []
            trade_payloads: list[dict] = []
            maker_pre_fill_anchors: list[dict] = []
            if crossing_book:
                deltas, trade_payloads, _, _, maker_pre_fill_anchors = await self._fast_apply_fill_settlement(
                    session=session,
                    market=market,
                    taker_snap=snap,
                    result=result,
                    executed_at=now,
                )
                for delta in deltas:
                    self.runtime.clearinghouse.apply_spot_delta(
                        int(delta["user_id"]),
                        str(delta["asset"]),
                        available_delta=Decimal(delta["available_delta"]),
                        frozen_delta=Decimal(delta["frozen_delta"]),
                    )
                snap["remaining_quantity"] = decimal_to_str(result.remaining_quantity)
                if Decimal(snap["filled_quantity"]) == ZERO:
                    snap["status"] = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
                elif Decimal(snap["remaining_quantity"]) == ZERO:
                    snap["status"] = ORDER_STATUS_FILLED
                else:
                    snap["status"] = ORDER_STATUS_PARTIALLY_FILLED
                if snap["status"] in {ORDER_STATUS_FILLED, ORDER_STATUS_CANCELED}:
                    self._fast_unregister_client_id(int(snap["user_id"]), int(market.id), snap.get("client_order_id"), order_id)
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
            else:
                snap["status"] = ORDER_STATUS_PARTIALLY_FILLED if filled > ZERO else ORDER_STATUS_NEW
                changed_bids = sequencer_result.amend_result.changed_bids
                changed_asks = sequencer_result.amend_result.changed_asks
            task = {
                "kind": "spot_amend",
                "symbol": market.symbol,
                "user_id": int(user.id),
                "order_id": order_id,
                "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload),
                "old_remaining": decimal_to_str(old_remaining),
                "new_remaining": decimal_to_str(new_remaining),
                "sequence_number": priority_sequence,
                "now": now.isoformat(),
                "amend_kind": "cross" if crossing_book else "simple",
                "engine_result": self._fast_engine_plan(result) if crossing_book else None,
                "ephemeral_robot_quote": user.role == ROLE_BOT,
                "maker_anchor_version": 1,
                "maker_pre_fill_anchors": maker_pre_fill_anchors,
            }
            await self._fast_enqueue_or_restore(session, market, task)
            self._fast_ingest_trade_payloads(
                market,
                trade_payloads,
                executed_at=now,
                taker_side=str(snap["side"]),
            )
        if broadcast:
            await self._fast_broadcast(market, changed_bids=changed_bids, changed_asks=changed_asks, trade_payloads=trade_payloads)
        self._update_metric(user, "amend_order_fast_ms", started)
        response = {
            "order": self._fast_serialize_order(market, snap),
            "kept_priority": False if crossing_book else (sequencer_result.amend_result.kept_priority if sequencer_result.amend_result is not None else False),
            "trades": trade_payloads,
            "fast_path": True,
        }
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": trade_payloads,
            }
        return response

    async def amend_order_batch_fast(
        self,
        session: AsyncSession,
        user: User,
        payload,
        *,
        broadcast: bool = True,
        market_override: Market | None = None,
        bbo_snapshot: dict[str, list[list[str]]] | None = None,
        include_orders: bool = True,
    ) -> dict | None:
        if not payload.orders or not self._fast_writer_ready():
            return None
        if any(item.order_id not in self._fast_orders for item in payload.orders):
            return None
        started = time.perf_counter()
        symbol = str(payload.symbol).upper()
        market = market_override or await self._get_market(session, symbol)
        now = datetime.now(tz=UTC)
        book = bbo_snapshot
        if book is None:
            book, _, _ = await self.runtime.orderbook_snapshot(market.symbol, 1)
        best_ask = Decimal(book["asks"][0][0]) if book.get("asks") else None
        best_bid = Decimal(book["bids"][0][0]) if book.get("bids") else None
        items: list[dict] = []
        failed: list[dict] = []
        prepared: list[dict] = []
        pending_delta: defaultdict[str, Decimal] = defaultdict(Decimal)
        for index, item in enumerate(payload.orders):
            try:
                snap = self._fast_orders.get(item.order_id)
                if snap is None or str(snap.get("symbol") or "").upper() != symbol:
                    raise OrderValidationError("order not found on fast book")
                if int(snap.get("user_id") or -1) != int(user.id):
                    raise OrderValidationError("cannot amend others order")
                new_price = self._normalize_price(
                    market,
                    item.price if item.price is not None else snap.get("price"),
                )
                new_quantity = self._normalize_qty(market, item.quantity)
                filled = self._normalize_qty(market, snap.get("filled_quantity") or ZERO)
                old_price = self._normalize_price(market, snap.get("price"))
                old_remaining = self._normalize_qty(market, snap.get("remaining_quantity") or ZERO)
                new_remaining = self._normalize_qty(market, new_quantity - filled)
                if old_price is None or new_price is None or new_remaining <= ZERO:
                    raise OrderValidationError("limit order price or quantity missing")
                crossing_book = (
                    (snap["side"] == SIDE_BUY and best_ask is not None and new_price >= best_ask)
                    or (snap["side"] == SIDE_SELL and best_bid is not None and new_price <= best_bid)
                )
                if crossing_book:
                    raise OrderValidationError("batch amend does not support crossing book")
                changed = not (new_price == old_price and new_quantity == Decimal(snap["quantity"]))
                delta = (
                    (new_price * new_remaining - old_price * old_remaining)
                    if snap["side"] == SIDE_BUY
                    else (new_remaining - old_remaining)
                )
                asset = market.quote_asset if snap["side"] == SIDE_BUY else market.base_asset
                if delta > ZERO:
                    if self.runtime.clearinghouse.spot_available(int(user.id), asset) - pending_delta[asset] < delta:
                        raise OrderValidationError("insufficient available balance")
                    pending_delta[asset] += delta
                prepared.append(
                    {
                        "index": index,
                        "snap": snap,
                        "new_price": new_price,
                        "new_quantity": new_quantity,
                        "new_remaining": new_remaining,
                        "old_remaining": old_remaining,
                        "delta": delta,
                        "asset": asset,
                        "filled": filled,
                        "changed": changed,
                    }
                )
            except (OrderValidationError, ValueError) as exc:
                failed.append({"index": index, "order_id": item.order_id, "error": str(exc)})

        changed_bids: list[list[str]] = []
        changed_asks: list[list[str]] = []
        async with self.runtime.fast_matching_guard(market.symbol):
            # Recheck engine membership under the same financial lock used by
            # FLOW and every fast mutation.  One missing order must not make
            # MatchingEngine reject the entire batch after balances have
            # already been adjusted.
            self._reconcile_ephemeral_spot_ghosts_locked(user=user, market=market)
            engine_book = self.runtime.engine.books.get(symbol)
            engine_order_ids = set(engine_book.orders) if engine_book is not None else set()
            still_prepared: list[dict] = []
            failed_indices = {int(item["index"]) for item in failed}
            for entry in prepared:
                order_id = str(entry["snap"]["order_id"])
                if order_id not in engine_order_ids:
                    if int(entry["index"]) not in failed_indices:
                        failed.append(
                            {
                                "index": entry["index"],
                                "order_id": order_id,
                                "error": "order not found on engine book",
                            }
                        )
                    continue
                still_prepared.append(entry)
            prepared = still_prepared
            batch_items = [
                BatchAmendItem(
                    order_id=str(entry["snap"]["order_id"]),
                    new_price=entry["new_price"],
                    new_remaining=entry["new_remaining"],
                )
                for entry in prepared
                if entry["changed"]
            ]

            net_balance_delta: defaultdict[str, Decimal] = defaultdict(Decimal)
            for entry in prepared:
                if entry["changed"]:
                    net_balance_delta[str(entry["asset"])] += Decimal(entry["delta"])
            balance_before: dict[str, tuple[object, Decimal, Decimal]] = {}
            for asset, delta in net_balance_delta.items():
                state = self.runtime.clearinghouse.spot_snapshot(int(user.id), asset)
                if state is None:
                    raise OrderValidationError(f"spot balance missing for asset {asset}")
                available = Decimal(state.available)
                frozen = Decimal(state.frozen)
                if delta > ZERO and available < delta - Decimal("1e-12"):
                    raise OrderValidationError("insufficient available balance")
                if delta < ZERO and frozen < -delta - Decimal("1e-12"):
                    raise OrderValidationError("insufficient frozen balance")
                balance_before[asset] = (state, available, frozen)

            batch = None
            try:
                for asset, delta in net_balance_delta.items():
                    if delta > ZERO:
                        self.runtime.clearinghouse.reserve_spot(int(user.id), asset, delta)
                    elif delta < ZERO:
                        self.runtime.clearinghouse.release_spot(int(user.id), asset, -delta)
                if batch_items:
                    sequencer_result = await self.runtime.get_symbol_sequencer(symbol).submit(
                        BatchAmendCommand(batch_items)
                    )
                    batch = sequencer_result.batch_amend_result
                    if batch is None:
                        raise OrderValidationError("sequencer did not return batch amend result")
                    missing_after_submit = [
                        item.order_id for item in batch_items if batch.results.get(item.order_id) is None
                    ]
                    if missing_after_submit:
                        raise OrderValidationError(
                            f"order not found on book: {missing_after_submit[0]}"
                        )
                    changed_bids = sequencer_result.changed_bids
                    changed_asks = sequencer_result.changed_asks
            except Exception:
                for state, available, frozen in balance_before.values():
                    state.available = available
                    state.frozen = frozen
                raise
            for entry in prepared:
                snap = entry["snap"]
                if entry["changed"]:
                    amend = batch.results.get(snap["order_id"]) if batch is not None else None
                    assert amend is not None
                    snap["price"] = decimal_to_str(entry["new_price"])
                    snap["quantity"] = decimal_to_str(entry["new_quantity"])
                    snap["remaining_quantity"] = decimal_to_str(entry["new_remaining"])
                    snap["sequence_number"] = int(amend.sequence_number or snap.get("sequence_number") or 0)
                    snap["updated_at"] = to_millis(now)
                    snap["version"] = int(snap.get("version") or 0) + 1
                task = {
                    "kind": "spot_amend",
                    "symbol": market.symbol,
                    "user_id": int(user.id),
                    "order_id": snap["order_id"],
                    "payload": {
                        "quantity": decimal_to_str(entry["new_quantity"]),
                        "price": decimal_to_str(entry["new_price"]),
                    },
                    "old_remaining": decimal_to_str(entry["old_remaining"]),
                    "new_remaining": decimal_to_str(entry["new_remaining"]),
                    "sequence_number": int(snap.get("sequence_number") or 0),
                    "now": now.isoformat(),
                    "amend_kind": "simple",
                    "engine_result": None,
                    "ephemeral_robot_quote": user.role == ROLE_BOT,
                    "maker_anchor_version": 1,
                }
                # sampled mode has no durable per-order business sink; the
                # in-memory mirror and matcher are authoritative for this
                # sandbox. Avoid re-entering the writer once per level in a
                # 100-level QuoteSet batch while retaining strict/memory
                # writer semantics outside sampled mode.
                if True:
                    await self._fast_enqueue_or_restore(session, market, task)
                self._fast_orders[snap["order_id"]] = snap
                self._fast_register_client_id(
                    int(snap["user_id"]), int(market.id), snap.get("client_order_id"), snap["order_id"]
                )
                item_result = {
                    "index": entry["index"],
                    "changed": bool(entry["changed"]),
                    "fast_path": True,
                }
                if include_orders:
                    item_result["order"] = self._fast_serialize_order(market, snap)
                items.append(item_result)

        if broadcast and (changed_bids or changed_asks):
            await self._fast_broadcast(
                market,
                changed_bids=changed_bids,
                changed_asks=changed_asks,
                trade_payloads=[],
            )
        self._update_metric(user, "amend_order_batch_fast_ms", started)
        response = {
            "ok": not failed,
            "requested_count": len(payload.orders),
            "amended_count": len(items),
            "failed_count": len(failed),
            "items": items,
            "failed": failed,
            "fast_path": True,
        }
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": [],
            }
        return response
