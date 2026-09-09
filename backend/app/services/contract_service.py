from __future__ import annotations

from app.services.matching_faults import BusinessRejected, NotExecuted

from app.services.maker_permissions import min_notional_exempt

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
import logging
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import (
    CONTRACT_TRADING_MODE_PAUSED,
    CONTRACT_TRADING_MODE_REDUCE_ONLY,
    MARGIN_MODE_ISOLATED,
    ORDER_STATUS_CANCELED,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    ORDER_STATUS_REJECTED,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    POSITION_ACTION_CLOSE,
    POSITION_ACTION_OPEN,
    POSITION_MODE_HEDGE,
    POSITION_MODE_ONE_WAY,
    POSITION_SIDE_FLAT,
    POSITION_SIDE_LONG,
    POSITION_SIDE_SHORT,
    PRODUCT_TYPE_PERP,
    ROLE_BOT,
    SIDE_BUY,
    SIDE_SELL,
    TIF_GTC,
    TIF_POST_ONLY,
    ZERO,
)
from app.core.decimal_utils import decimal_to_str, is_step_aligned, quantize_scale, to_decimal
from app.core.time_utils import ensure_utc, to_millis
from app.models.contract_account import ContractAccount
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_position import ContractPosition
from app.models.contract_risk_limit_tier import ContractRiskLimitTier
from app.models.contract_user_setting import ContractUserSetting
from app.models.fee_profile import FeeProfile
from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.services.contract_netting import split_fill, required_reserves
from app.services.ids import next_liquidation_id, next_order_id, next_trade_id
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.contract_insurance import cover_contract_bad_debt
from app.services.clearinghouse import PositionState
from app.services.matching_engine import BookOrder, EngineResult, MatchFill
from app.services.order_service import BOT_ORDER_PREFIXES, engine_result_from_plan
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
from app.services.financial_outbox_service import enqueue_financial_outbox
from app.services.synthetic_flow_service import (
    SyntheticFlowValidationError,
    SyntheticFlowUnavailableError,
    broadcast_synthetic_flow,
    execute_synthetic_flow,
)
from app.services.persistence_contract import (
    is_paper_robot_topup_eligible,
    is_runtime_only_mode,
    platform_durable_contract,

)
from exchange_common.quote_pipeline import SelfTradePolicy


CONTRACT_DEMO_WALLET = Decimal("1000000")
CONTRACT_EPSILON = Decimal("1e-10")


class ContractValidationError(BusinessRejected):
    pass


@dataclass(slots=True)
class ContractReservePlan:
    amount: Decimal
    leverage: Decimal


@dataclass(slots=True)
class ContractRiskTierSnapshot:
    tier: int
    notional_floor: Decimal
    notional_cap: Decimal | None
    max_leverage: Decimal
    maintenance_margin_rate: Decimal
    maintenance_amount: Decimal


class ContractService:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime
        self._ladder_bindings: set[tuple[int, int]] = set()
        self._ladder_terminal_orders: OrderedDict[str, dict] = OrderedDict()
        self._ladder_financial_unknown: dict[str, str] = {}
        self._fast_contract_orders: dict[str, dict] = {}
        self._fast_contract_client_ids: dict[tuple[int, int, str], str] = {}
        self._fast_broadcast_scheduler = LatestWinsTaskScheduler(
            self._consume_fast_broadcast,
            name="perp-fast-broadcast",
        )
        self._fast_metrics: dict[str, int] = {
            "fast_place": 0,
            "fast_amend": 0,
            "fast_cancel": 0,
            "fast_batch_amend": 0,
            "fast_batch_place": 0,
            "amend_success": 0,
            "amend_failed": 0,
            "fallback_replace": 0,
            "batch_amend_calls": 0,
            "batch_place_calls": 0,
            "ephemeral_ghost_evictions": 0,
            "slow_place": 0,
            "slow_amend": 0,
            "slow_cancel": 0,
        }

    def invalidate_internal_maker_bindings(self, market_id: int) -> None:
        """After a committed account-role change, require fresh server binding checks."""
        self._ladder_bindings.difference_update({key for key in self._ladder_bindings if key[1] == int(market_id)})

    async def is_contract_ladder(self, session: AsyncSession, user_id: int, market_id: int) -> bool:
        """Server-owned identity; client tags never grant internal account semantics."""
        key = (int(user_id), int(market_id))
        if key in self._ladder_bindings:
            return True
        if not platform_durable_contract():
            return False
        bound = await session.scalar(
            select(MarketBotAccount.id).join(User, User.id == MarketBotAccount.user_id).where(
                MarketBotAccount.user_id == key[0], MarketBotAccount.market_id == key[1],
                MarketBotAccount.strategy_role == "CONTRACT_LADDER", User.role == ROLE_BOT,
            ).limit(1)
        )
        if bound is not None:
            self._ladder_bindings.add(key)
            return True
        return False

    def remember_ladder_order(self, snap: dict) -> None:
        order_id = str(snap["order_id"])
        if str(snap["status"]) in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED} and Decimal(snap["remaining_quantity"]) > ZERO:
            self._fast_contract_orders[order_id] = dict(snap)
            self._fast_register_client_id(int(snap["user_id"]), int(snap["market_id"]), snap.get("client_order_id"), order_id)
        else:
            self._fast_contract_orders.pop(order_id, None)
            self._fast_unregister_client_id(int(snap["user_id"]), int(snap["market_id"]), snap.get("client_order_id"), order_id)
            self._ladder_terminal_orders[order_id] = dict(snap)
            self._ladder_terminal_orders.move_to_end(order_id)
            while len(self._ladder_terminal_orders) > 4096:
                self._ladder_terminal_orders.popitem(last=False)

    async def ensure_ladder_order_anchor(self, session: AsyncSession, market: Market, snap: dict) -> Order:
        """Materialize only the internal order identity needed by an actual user fill."""
        if int(snap.get("market_id") or 0) != int(market.id) or not await self.is_contract_ladder(session, int(snap["user_id"]), market.id):
            raise ContractValidationError("untrusted internal maker anchor")
        row = await session.scalar(select(Order).where(Order.order_id == snap["order_id"]))
        if row is None:
            row = Order(order_id=snap["order_id"], user_id=int(snap["user_id"]), market_id=int(market.id),
                        product_type=PRODUCT_TYPE_PERP, created_at=datetime.fromtimestamp(int(snap.get("created_at") or to_millis(datetime.now(tz=UTC))) / 1000, tz=UTC))
            session.add(row)
        elif int(row.user_id) != int(snap["user_id"]) or int(row.market_id) != int(market.id):
            raise ContractValidationError("internal maker anchor identity conflict")
        for field in ("client_order_id", "side", "type", "tif", "position_action", "status", "reduce_only"):
            setattr(row, field, snap.get(field))
        for field in ("price", "quantity", "filled_quantity", "remaining_quantity", "avg_price", "notional", "leverage", "reference_price"):
            value = snap.get(field)
            setattr(row, field, Decimal(str(value)) if value is not None else None)
        row.version = int(snap.get("version") or 0)
        row.sequence_number = int(snap.get("sequence_number") or 0)
        row.updated_at = datetime.now(tz=UTC)
        row.canceled_at = None
        await session.flush()
        return row

    async def preflight_ladder_user_fills(self, session: AsyncSession, market: Market, fills, *, taker_order=None) -> None:
        """Check position transitions before matching, under the financial guard.

        Resting orders can outlive the position state that admitted them. Only
        the candidate fills' users are read; scalar projections never dirty ORM
        rows or re-run matching, and internal LADDER accounts have no position.
        """
        if not fills:
            return
        orders = await self.load_orders_map(session, {fill.maker_order_id for fill in fills})
        taker_id = taker_order.order_id if taker_order is not None else None
        if taker_order is not None and not await self.is_contract_ladder(session, taker_order.user_id, market.id):
            orders[taker_id] = taker_order
            expanded = []
            for fill in fills:
                expanded.append(fill)
                expanded.append(SimpleNamespace(maker_order_id=taker_id, maker_user_id=taker_order.user_id,
                    price=fill.price, quantity=fill.quantity))
            fills = expanded
        fills = [fill for fill in fills if not await self.is_contract_ladder(session, fill.maker_user_id, market.id)]
        users = {int(fill.maker_user_id) for fill in fills}
        modes = dict((await session.execute(
            select(ContractUserSetting.user_id, ContractUserSetting.position_mode).where(
                ContractUserSetting.market_id == market.id, ContractUserSetting.user_id.in_(users)
            )
        )).all())
        positions = (await session.execute(
            select(ContractPosition.user_id, ContractPosition.side, ContractPosition.quantity,
                   ContractPosition.entry_price, ContractPosition.isolated_margin).where(
                ContractPosition.market_id == market.id, ContractPosition.user_id.in_(users),
                *ContractPosition.active_filters(),
            )
        )).all()
        projected = {}
        finances = {}
        for uid in users:
            account = await session.scalar(select(ContractAccount).where(
                ContractAccount.user_id == uid, ContractAccount.margin_asset == (market.margin_asset or market.quote_asset)))
            if account is None:
                raise ContractValidationError("maker account is not ready")
            finances[uid] = [Decimal(account.wallet_balance), Decimal(account.used_margin), Decimal(account.unrealized_pnl)]
        details = {}
        rates = {}
        for uid, side, quantity, entry, margin in positions:
            key = (int(uid), side if modes.get(uid) == POSITION_MODE_HEDGE else None)
            if key in projected:
                raise ContractValidationError("maker position mode conflicts with current positions")
            projected[key] = (side, self._position_qty_without_storage_dust(market, quantity))
            details[key] = (Decimal(entry), Decimal(margin))
        consumed = {}
        for fill in fills:
            try:
                order = orders.get(fill.maker_order_id)
                if (order is None or int(order.user_id) != int(fill.maker_user_id)
                        or int(order.market_id) != int(market.id) or order.product_type != PRODUCT_TYPE_PERP):
                    raise ContractValidationError("maker financial state is not ready")
                if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                    raise ContractValidationError("maker order is no longer live")
                consumed[order.order_id] = consumed.get(order.order_id, ZERO) + fill.quantity
                if consumed[order.order_id] > self._position_qty_without_storage_dust(market, order.remaining_quantity):
                    raise ContractValidationError("maker financial leaves are not ready")
                action = order.position_action or POSITION_ACTION_OPEN
                target = self.order_position_side(order.side, action)
                key = (int(order.user_id), target if modes.get(order.user_id) == POSITION_MODE_HEDGE else None)
                side, quantity = projected.get(key, (POSITION_SIDE_FLAT, ZERO))
                try:
                    split = split_fill(side=order.side, action=action, reduce_only=bool(order.reduce_only),
                        hedge=modes.get(order.user_id) == POSITION_MODE_HEDGE,
                        position_side=side, position_qty=quantity, quantity=fill.quantity)
                except ValueError as exc:
                    raise ContractValidationError(str(exc)) from exc
                if not market.is_active or market.contract_trading_mode == CONTRACT_TRADING_MODE_PAUSED:
                    raise ContractValidationError("contract market is paused or inactive")
                restricted = (market.contract_trading_mode == CONTRACT_TRADING_MODE_REDUCE_ONLY or
                    (market.is_listed and market.paper_status in {"REDUCE_ONLY", "DELISTING"}))
                if split.opened and restricted:
                    raise ContractValidationError("contract market is reduce-only")
                entry, margin = details.get(key, (ZERO, ZERO))
                mark = self.mark_price(market)
                sign = 1 if side == POSITION_SIDE_LONG else -1
                old_unrealized = (mark - entry) * quantity * sign if quantity else ZERO
                realized = (fill.price - entry) * split.close * sign
                released = margin * split.close / quantity if quantity else ZERO
                added = fill.price * split.opened / Decimal(order.leverage or market.default_leverage)
                rate_key = (order.user_id, order.order_id == taker_id)
                if rate_key not in rates:
                    rates[rate_key] = await self.get_fee_rate(session, order.user_id, market.id, taker=rate_key[1], market=market)
                fee = fill.price * fill.quantity * rates[rate_key]
                cash = finances[order.user_id]
                cash[0] += realized - fee
                cash[1] += added - released - self.reserved_margin_for_fill(order, fill.quantity, fill.price)
                remaining = quantity - split.close
                new_qty = remaining + split.opened
                new_entry = ((entry * remaining + fill.price * split.opened) / new_qty) if new_qty else ZERO
                new_side = self.order_position_side(order.side, POSITION_ACTION_OPEN) if split.opened else side
                new_unrealized = (mark-new_entry)*new_qty*(1 if new_side == POSITION_SIDE_LONG else -1) if new_qty else ZERO
                cash[2] += new_unrealized - old_unrealized
                if split.opened and cash[0] + cash[2] - cash[1] < -CONTRACT_EPSILON:
                    raise ContractValidationError("insufficient margin for projected fill and fee")
                if cash[0] < -CONTRACT_EPSILON:
                    raise ContractValidationError("insufficient wallet for projected fill and fee")
                if split.opened:
                    tier = await self.risk_tier_for_notional(session, market, max(mark, fill.price)*new_qty)
                    if Decimal(order.leverage or market.default_leverage) > min(Decimal(market.max_leverage), Decimal(tier.max_leverage)):
                        raise ContractValidationError("projected fill exceeds risk tier leverage")
                details[key] = (new_entry, margin - released + added)
                opened_side = self.order_position_side(order.side, POSITION_ACTION_OPEN)
                projected[key] = ((opened_side, remaining + split.opened) if split.opened else
                                  (side, remaining) if remaining > CONTRACT_EPSILON else (POSITION_SIDE_FLAT, ZERO))
            except ContractValidationError as exc:
                exc.order_id = fill.maker_order_id
                raise

    @staticmethod
    def _expire_financial_reads(session):
        for obj in list(session.identity_map.values()):
            if isinstance(obj, (ContractAccount, ContractPosition, ContractUserSetting, Order)) and obj not in session.dirty:
                session.expire(obj)

    async def prepare_resting_fills(self, session, market, preview):
        """Under financial + writer admission, retire an unexecutable candidate.

        Must run BEFORE creating/reserving a taker. Each cancellation is a
        confirmed standalone lifecycle transaction; reacquire the writer and
        project again before allowing a subsequent matching command.
        """
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission
        while True:
            fills = [f for f in preview() if not await self.is_contract_ladder(session, f.maker_user_id, market.id)]
            try:
                await self.preflight_ladder_user_fills(session, market, fills)
                return
            except ContractValidationError as exc:
                oid = getattr(exc, "order_id", None)
                if oid is None or not market.is_active or market.contract_trading_mode == CONTRACT_TRADING_MODE_PAUSED:
                    raise
                order = await session.scalar(select(Order).where(Order.order_id == oid))
                if order is None:
                    raise
                account = await self.get_account(session, order.user_id, market.margin_asset or market.quote_asset)
                before = snapshot_contract_account(account)
                if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                    release = self.reserved_margin_for_fill(order, Decimal(order.remaining_quantity), Decimal(order.price or order.reference_price or ZERO))
                    account.used_margin = Decimal(account.used_margin) - release
                    order.reserved_margin = ZERO
                    order.status = ORDER_STATUS_CANCELED
                    order.reject_reason = str(exc)[:128]
                    order.canceled_at = datetime.now(tz=UTC)
                    order.updated_at = order.canceled_at
                    order.version = int(order.version or 0) + 1
                    await self.refresh_account(session, account)
                    await add_contract_ledger_entry(session, account, change_type="margin_release", amount=ZERO,
                        before=before, market_id=market.id, related_order_id=oid, note="contract_unexecutable_leaves_cancel")
                # Commit facts first: failure cannot silently remove a book order.
                await session.commit()
                side_name, _, changes = self.runtime.engine.cancel_order(market.symbol, oid)
                self._fast_contract_orders.pop(oid, None)
                self._fast_unregister_client_id(order.user_id, market.id, order.client_order_id, oid)
                self.runtime.acknowledge_committed_lifecycle(market.symbol)
                try:
                    await self.broadcast_order_flow(session, market.symbol, [order], {order.user_id},
                        changes if side_name == SIDE_BUY else [], changes if side_name == SIDE_SELL else [], [])
                except Exception:
                    logging.getLogger("contract_service").exception("confirmed lifecycle cancel broadcast failed: %s", oid)
                await acquire_sqlite_write_admission(session, existing_order_id=oid)
                market = await session.get(Market, market.id, populate_existing=True)

    def _ordinary_preview(self, market, payload, user):
        context = self._stp_context(user)
        return self.runtime.engine.ensure_market(market.symbol).preview_bbo_order(
            side=payload.side, quantity=Decimal(payload.quantity),
            limit_price=Decimal(payload.price) if payload.price is not None else Decimal("Infinity") if payload.side == SIDE_BUY else ZERO,
            taker_account_key=context["stp_account_key"], taker_is_bot=context["stp_is_bot"],
            stp_policy=context["stp_policy"]).fills

    async def _preflight_user_makers(self, session, market, *, side, quantity, price,
                                    margin_guard=False, stp_context=None, taker_order=None) -> None:
        """Reject a stale resting position before a normal user command matches."""
        book = self.runtime.engine.ensure_market(market.symbol)
        best = book._best_level(SIDE_SELL if side == SIDE_BUY else SIDE_BUY)
        if best is None or (price is not None and (
                (side == SIDE_BUY and best[0] > price) or (side == SIDE_SELL and best[0] < price))):
            return
        context = stp_context or {}
        preview = book.preview_bbo_order(side=side, quantity=quantity,
            limit_price=price if price is not None else Decimal("Infinity") if side == SIDE_BUY else ZERO,
            maker_guard=(lambda _oid, uid, _price, _qty: self._contract_maker_margin_ok(uid, int(market.id))) if margin_guard else None,
            taker_account_key=context.get("stp_account_key"), taker_is_bot=context.get("stp_is_bot", False),
            stp_policy=context.get("stp_policy") or SelfTradePolicy(same_account_mode=context.get("stp_mode", "cancel_taker")))
        # The preview stops at an insolvent maker; the live matcher may remove
        # it and proceed farther. Do not execute a financial range not checked.
        if preview.stop_reason == "maker_not_authorized_for_synthetic_flow":
            raise NotExecuted("resting maker is not currently settleable; matching command not executed")
        fills = list(preview.fills)
        try:
            if taker_order is None:
                fills = [f for f in fills if not await self.is_contract_ladder(session, f.maker_user_id, market.id)]
                await self.preflight_ladder_user_fills(session, market, fills)
            else:
                await self.preflight_ladder_user_fills(session, market, fills, taker_order=taker_order)
        except ContractValidationError as exc:
            raise NotExecuted(f"resting maker is not currently settleable: {exc}; matching command not executed") from exc

    async def _consume_fast_broadcast(self, _symbol: str, payload: dict) -> None:
        await self._deferred_fast_broadcast_contract(**payload)

    @staticmethod
    def _merge_fast_broadcast_payload(previous: dict, latest: dict) -> dict:
        merged = dict(previous)
        merged["snapshot"] = latest["snapshot"]
        merged["seq"] = latest["seq"]
        merged["updated_at_ms"] = latest["updated_at_ms"]
        merged["changed_bids"] = latest["changed_bids"]
        merged["changed_asks"] = latest["changed_asks"]
        merged["trade_payloads"] = (list(previous.get("trade_payloads") or []) + list(latest.get("trade_payloads") or []))[-512:]
        merged["order_snaps"] = (list(previous.get("order_snaps") or []) + list(latest.get("order_snaps") or []))[-512:]
        merged["impacted_users"] = set(previous.get("impacted_users") or set()) | set(latest.get("impacted_users") or set())
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

    async def _load_live_client_order(self, session: AsyncSession, user: User, market: Market, client_order_id: str | None) -> Order | None:
        if not client_order_id:
            return None
        return await session.scalar(
            select(Order)
            .where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.client_order_id == client_order_id,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1)
        )

    @staticmethod
    def _live_client_order_matches(order: Order, payload) -> bool:
        order_price = Decimal(order.price) if order.price is not None else None
        payload_price = Decimal(payload.price) if payload.price is not None else None
        return (
            order.side == payload.side
            and order.type == payload.type
            and order.tif == payload.tif
            and order.position_action == payload.position_action
            and bool(order.reduce_only) == bool(payload.reduce_only)
            and Decimal(order.quantity) == Decimal(payload.quantity)
            and order_price == payload_price
        )

    @staticmethod
    def _normalize_price(market: Market, value: Decimal | str | int | float | None) -> Decimal | None:
        if value is None:
            return None
        return quantize_scale(value, market.price_precision)

    @staticmethod
    def _normalize_qty(market: Market, value: Decimal | str | int | float) -> Decimal:
        return quantize_scale(value, market.qty_precision)

    @staticmethod
    def _position_qty_without_storage_dust(market: Market, value) -> Decimal:
        """Restore only sub-step SQLite REAL noise, never round a real shortfall."""
        raw = to_decimal(value)
        normalized = quantize_scale(raw, market.qty_precision)
        quantum = Decimal(1).scaleb(-market.qty_precision)
        tolerance = min(CONTRACT_EPSILON, quantum * Decimal("0.000001"))
        return normalized if abs(normalized - raw) <= tolerance else raw

    def _normalize_payload(self, market: Market, payload) -> None:
        if getattr(payload, "reduce_only", False) or getattr(payload, "position_action", None) == POSITION_ACTION_CLOSE:
            payload.position_action = POSITION_ACTION_CLOSE
            payload.reduce_only = True
        # Integer markets reject fractions before normalization; never change the requested amount.
        if market.qty_precision == 0 and to_decimal(payload.quantity) != to_decimal(payload.quantity).to_integral_value():
            raise ContractValidationError("quantity must be an integer for this market")
        if market.price_precision == 0 and payload.price is not None and to_decimal(payload.price) != to_decimal(payload.price).to_integral_value():
            raise ContractValidationError("price must be an integer for this market")
        payload.quantity = self._normalize_qty(market, payload.quantity)
        if payload.price is not None:
            payload.price = self._normalize_price(market, payload.price)

    async def load_open_orders(self, session: AsyncSession) -> None:
        query = (
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(
                Order.product_type == PRODUCT_TYPE_PERP,
                Market.product_type == PRODUCT_TYPE_PERP,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
            )
            .order_by(Order.sequence_number.asc(), Order.created_at.asc(), Order.id.asc())
        )
        if settings.persistence_mode == "memory":
            query = query.join(User, User.id == Order.user_id).where(User.role != "mm_bot")
        result = await session.execute(query)
        for order, market in result.all():
            remaining = self._normalize_qty(market, order.remaining_quantity)
            price = self._normalize_price(market, order.price)
            self.runtime.observe_sequence(market.symbol, order.sequence_number)
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
                    created_at=order.created_at,
                    sequence_number=int(order.sequence_number or 0),
                    stp_account_key=f"user:{int(order.user_id)}",
                    stp_group_key="bot" if str(order.order_id).startswith(BOT_ORDER_PREFIXES) else "user",
                    stp_is_bot=str(order.order_id).startswith(BOT_ORDER_PREFIXES),
                    stp_mode=settings.stp_same_account_mode,
                ),
            )

    async def open_order_ids_for_market(self, session: AsyncSession, market: Market) -> set[str]:
        rows = await session.execute(
            select(Order.order_id).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
            )
        )
        # Financial order rows are authoritative for users. Explicit internal
        # ladder quotes are restart-ephemeral and their complete live mirror is
        # authoritative until the coordinator confirms a terminal engine result.
        # Do not silently erase a mirror/engine mismatch: the adapter must keep
        # that evidence and pause UNKNOWN instead of calling the book healthy.
        order_ids = set(rows.scalars())
        if platform_durable_contract():
            for order_id, snap in list(self._fast_contract_orders.items()):
                if (int(snap.get("market_id") or -1) == int(market.id)
                        and str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                        and Decimal(snap.get("remaining_quantity") or 0) > ZERO
                        and await self.is_contract_ladder(session, int(snap["user_id"]), int(market.id))):
                    order_ids.add(str(order_id))
        return order_ids

    async def load_fast_path_state(self, session: AsyncSession) -> None:
        """Load PERP open GTC limit orders into the in-memory fast-path mirror."""
        query = (
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(
                Order.product_type == PRODUCT_TYPE_PERP,
                Market.product_type == PRODUCT_TYPE_PERP,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
            )
        )
        if settings.persistence_mode == "memory":
            query = query.join(User, User.id == Order.user_id).where(User.role != "mm_bot")
        rows = await session.execute(query)
        for order, market in rows.all():
            snap = self._fast_order_snapshot(order, market)
            self._fast_contract_orders[order.order_id] = snap
            if order.client_order_id:
                self._fast_contract_client_ids[(int(order.user_id), int(market.id), str(order.client_order_id))] = order.order_id

    def fast_metrics_snapshot(self) -> dict:
        return dict(self._fast_metrics)

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
            raise ContractValidationError("persistence writer unavailable")
        from app.services.persistence_contract import facts_durable

        if callable(getattr(writer, "is_capturing", None)) and writer.is_capturing():
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
            await self._fast_restore_market(
                session,
                int(market.id),
                reason="causal_durable_execution_failed",
            )
            raise

    def _fast_register_client_id(self, user_id: int, market_id: int, client_order_id: str | None, order_id: str) -> None:
        if client_order_id:
            self._fast_contract_client_ids[(int(user_id), int(market_id), str(client_order_id))] = order_id

    def _fast_unregister_client_id(self, user_id: int, market_id: int, client_order_id: str | None, order_id: str) -> None:
        if not client_order_id:
            return
        key = (int(user_id), int(market_id), str(client_order_id))
        if self._fast_contract_client_ids.get(key) == order_id:
            self._fast_contract_client_ids.pop(key, None)

    def _evict_ephemeral_contract_ghost(
        self,
        *,
        user: User,
        market: Market,
        order_id: str,
        expected_snap: dict,
    ) -> bool:
        """Drop a proven engine-absent sandbox bot quote from the fast mirror.

        The caller must hold ``clearinghouse.global_lock`` and must only invoke
        this after the matching engine explicitly reports that the order does
        not exist.  A second writer-ready check prevents a HALT/full-queue
        condition from being mistaken for proof that a mirror entry is stale.
        """
        if (
            (settings.persistence_mode != "memory" and not platform_durable_contract())
            or str(user.role) != ROLE_BOT
            or not self._fast_writer_ready()
            or self._fast_contract_orders.get(order_id) is not expected_snap
        ):
            return False
        self._fast_unregister_client_id(
            int(expected_snap["user_id"]),
            int(market.id),
            expected_snap.get("client_order_id"),
            order_id,
        )
        self._fast_contract_orders.pop(order_id, None)
        self._fast_metrics["ephemeral_ghost_evictions"] += 1
        return True

    def _fast_order_snapshot(self, order: Order, market: Market) -> dict:
        return {
            "order_id": order.order_id,
            "client_order_id": order.client_order_id,
            "user_id": int(order.user_id),
            "market_id": int(market.id),
            "symbol": market.symbol,
            "product_type": order.product_type,
            "side": order.side,
            "position_action": order.position_action,
            "reduce_only": bool(order.reduce_only),
            "reserved_margin": str(order.reserved_margin) if order.reserved_margin is not None else None,
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
            "reject_reason": order.reject_reason,
            "created_at": to_millis(order.created_at),
            "updated_at": to_millis(order.updated_at),
        }

    def _fast_serialize_order(self, market: Market, snap: dict) -> dict:
        price = snap.get("price")
        return {
            "order_id": snap["order_id"],
            "client_order_id": snap.get("client_order_id"),
            "symbol": market.symbol,
            "product_type": market.product_type,
            "side": snap["side"],
            "position_action": snap["position_action"],
            "reduce_only": bool(snap.get("reduce_only")),
            "leverage": snap.get("leverage"),
            "type": snap["type"],
            "tif": snap["tif"],
            "status": snap["status"],
            "sequence_number": int(snap.get("sequence_number") or 0),
            "version": int(snap.get("version") or 0),
            "price": price,
            "quantity": snap["quantity"],
            "filled_quantity": snap.get("filled_quantity") or "0",
            "remaining_quantity": snap["remaining_quantity"],
            "avg_price": snap.get("avg_price"),
            "notional": snap.get("notional"),
            "reject_reason": snap.get("reject_reason"),
            "created_at": snap.get("created_at"),
            "updated_at": snap.get("updated_at"),
        }

    async def contract_financial_keys(self, session: AsyncSession, market: Market, *extra_user_ids: int) -> list[str]:
        rows = await session.execute(
            select(Order.user_id).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            ).distinct()
        )
        user_ids = {int(user_id) for user_id in rows.scalars()}
        user_ids.update(int(user_id) for user_id in extra_user_ids)
        asset = (market.margin_asset or market.quote_asset).upper()
        return [f"contract:{user_id}:{asset}" for user_id in sorted(user_ids)]

    async def reconcile_engine_book(self, session: AsyncSession, market: Market) -> tuple[list[list[str]], list[list[str]], list[str]]:
        if is_runtime_only_mode():
            return [], [], []
        writer = getattr(self.runtime, "persistence_writer", None)
        if writer is not None:
            lag = getattr(writer, "materialization_lag", None)
            if (writer._busy or not writer._queue.empty()) or (
                callable(lag) and lag() > 0
            ):
                # A WAL reader cannot see the writer's in-flight transaction and
                # pending events are not yet materialized. Pruning against that
                # snapshot would drop just-placed fast-path orders from the
                # engine/mirror, so skip reconciliation until the event log has
                # fully materialized.
                return [], [], []
        valid_order_ids = await self.open_order_ids_for_market(session, market)
        if platform_durable_contract():
            # 与现货侧一致：策略机器人报价是重启重建的临时态，耐用 DB 只保留
            # 生命周期锚点；把快速镜像里的活跃机器人报价并入有效集合，避免
            # 引擎裁剪误删镜像报价导致 QuoteSet 撤单永久失败。
            valid_order_ids = set(valid_order_ids)
            for order_id, snap in self._fast_contract_orders.items():
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
                reason="contract_engine_only_prune",
                engine_only_removed=len(removed),
            )
        return changed_bids, changed_asks, removed

    async def load_book_orders_from_db(self, session: AsyncSession, market: Market) -> list[BookOrder]:
        rows = await session.execute(
            select(Order)
            .where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
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

    async def rebuild_engine_book_from_db(self, session: AsyncSession, market_id: int, *, reason: str) -> dict:
        writer = getattr(self.runtime, "persistence_writer", None)
        catchup = getattr(writer, "materialize_catchup", None)
        if callable(catchup):
            await catchup(timeout=10.0)
        market = await session.scalar(select(Market).where(Market.id == market_id))
        if market is None:
            return {}
        if market.symbol in self._ladder_financial_unknown:
            raise ContractValidationError("cannot rebuild unresolved internal user settlement from DB")
        book_orders = await self.load_book_orders_from_db(session, market)
        if platform_durable_contract():
            live_internal_ids = {
                order_id for order_id, snap in list(self._fast_contract_orders.items())
                if int(snap.get("market_id") or -1) == int(market.id)
                and str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                and await self.is_contract_ladder(session, int(snap["user_id"]), int(market.id))
            }
            # A prior user fill may have persisted an older lifecycle anchor.
            # Rebuilding compatibility state must preserve current internal
            # leaves/price, not resurrect that historical remaining quantity.
            book_orders = [row for row in book_orders if row.order_id not in live_internal_ids]

            # 重建引擎时同样要把快速镜像里的活跃机器人报价放回盘口：
            # 耐用 DB 只保留报价生命周期锚点，不含全部临时档位。
            existing = {str(item.order_id) for item in book_orders}
            for snap in self._fast_contract_orders.values():
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

    async def commit_or_rebuild_engine(self, session: AsyncSession, market: Market, *, reason: str) -> None:
        market_id = int(market.id)
        market_symbol = str(market.symbol)
        try:
            transaction = session.sync_session.get_transaction()
            await session.commit()
            chart_batch = session.info.pop("contract_chart_batch", None)
            if chart_batch is not None and chart_batch[0] is transaction:
                for chart_trade in chart_batch[1]:
                    self.runtime.market_data.ingest_trade(**chart_trade)
            self.runtime.publish_orderbook_snapshot_unlocked(market.symbol)
        except Exception:
            session.info.pop("contract_chart_batch", None)
            await session.rollback()
            if any(mid == market_id for _uid, mid in self._ladder_bindings):
                self._ladder_financial_unknown[market_symbol] = reason
            else:
                await self.rebuild_engine_book_from_db(session, market_id, reason=reason)
            raise

    async def get_market(self, session: AsyncSession, symbol: str) -> Market:
        market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
        if market is None:
            raise ContractValidationError("market not found")
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ContractValidationError("market is not a PERP contract")
        return market

    @staticmethod
    def fallback_risk_tier(market: Market) -> ContractRiskTierSnapshot:
        return ContractRiskTierSnapshot(
            tier=1,
            notional_floor=ZERO,
            notional_cap=None,
            max_leverage=Decimal(market.max_leverage),
            maintenance_margin_rate=Decimal(market.maintenance_margin_rate),
            maintenance_amount=ZERO,
        )

    @staticmethod
    def serialize_risk_tier(tier: ContractRiskLimitTier | ContractRiskTierSnapshot, market: Market) -> dict:
        return {
            "symbol": market.symbol,
            "tier": int(tier.tier),
            "notional_floor": decimal_to_str(quantize_scale(tier.notional_floor, 8)),
            "notional_cap": decimal_to_str(quantize_scale(tier.notional_cap, 8)) if tier.notional_cap is not None else None,
            "max_leverage": decimal_to_str(to_decimal(tier.max_leverage)),
            "maintenance_margin_rate": decimal_to_str(to_decimal(tier.maintenance_margin_rate)),
            "maintenance_amount": decimal_to_str(quantize_scale(to_decimal(tier.maintenance_amount), 8)),
        }

    @staticmethod
    def maintenance_margin_for_notional(notional: Decimal, risk_tier: ContractRiskLimitTier | ContractRiskTierSnapshot) -> Decimal:
        rate = to_decimal(risk_tier.maintenance_margin_rate)
        amount = to_decimal(risk_tier.maintenance_amount)
        return max((to_decimal(notional) * rate) - amount, ZERO)

    async def risk_tiers_for_market(self, session: AsyncSession, market: Market) -> list[ContractRiskLimitTier]:
        rows = await session.execute(
            select(ContractRiskLimitTier)
            .where(ContractRiskLimitTier.market_id == market.id)
            .order_by(ContractRiskLimitTier.tier.asc())
        )
        return list(rows.scalars())

    async def risk_tier_for_notional(
        self,
        session: AsyncSession | None,
        market: Market,
        notional: Decimal,
    ) -> ContractRiskLimitTier | ContractRiskTierSnapshot:
        if session is None:
            return self.fallback_risk_tier(market)
        with session.no_autoflush:
            tiers = await self.risk_tiers_for_market(session, market)
        if not tiers:
            return self.fallback_risk_tier(market)
        target = max(to_decimal(notional), ZERO)
        selected = tiers[-1]
        for tier in tiers:
            floor = to_decimal(tier.notional_floor)
            cap = to_decimal(tier.notional_cap) if tier.notional_cap is not None else None
            if target >= floor and (cap is None or target < cap):
                selected = tier
                break
        return selected

    async def get_account(self, session: AsyncSession, user_id: int, margin_asset: str = "USDT") -> ContractAccount:
        asset = margin_asset.upper().strip() or "USDT"
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == user_id,
                ContractAccount.margin_asset == asset,
            )
        )
        if account is None:
            account = ContractAccount(
                user_id=user_id,
                margin_asset=asset,
                wallet_balance=CONTRACT_DEMO_WALLET,
                available_margin=CONTRACT_DEMO_WALLET,
                used_margin=ZERO,
                unrealized_pnl=ZERO,
                realized_pnl=ZERO,
                total_fees=ZERO,
                account_run_id=await session.scalar(select(User.current_account_run_id).where(User.id == user_id)),
            )
            session.add(account)
            await session.flush()
            await add_contract_ledger_entry(
                session,
                account,
                change_type="account_init",
                amount=CONTRACT_DEMO_WALLET,
                before={
                    "wallet": ZERO,
                    "available": ZERO,
                    "used_margin": ZERO,
                    "unrealized_pnl": ZERO,
                    "realized_pnl": ZERO,
                    "total_fees": ZERO,
                },
                note="demo_contract_wallet",
                skip_if_unchanged=False,
            )
        return account

    async def adjust_account(
        self,
        session: AsyncSession,
        *,
        user_id: int,
        margin_asset: str,
        amount: Decimal,
    ) -> ContractAccount:
        asset = margin_asset.upper()
        async with self.runtime.financial_resources([f"contract:{user_id}:{asset}"]):
            account = await self.get_account(session, user_id, asset)
            before = snapshot_contract_account(account)
            account.wallet_balance = Decimal(account.wallet_balance) + amount
            if account.wallet_balance < ZERO:
                raise ContractValidationError("contract wallet would become negative")
            await self.refresh_account(session, account)
            await add_contract_ledger_entry(
                session,
                account,
                change_type="admin_adjust",
                amount=amount,
                before=before,
                note="admin_contract_account_adjust",
            )
            await session.commit()
            return account

    async def get_setting(self, session: AsyncSession, user_id: int, market: Market, *, persist: bool = True) -> ContractUserSetting | SimpleNamespace:
        setting = await session.scalar(
            select(ContractUserSetting).where(
                ContractUserSetting.user_id == user_id,
                ContractUserSetting.market_id == market.id,
            )
        )
        if not persist:
            return SimpleNamespace(
                leverage=setting.leverage if setting is not None else market.default_leverage,
                margin_mode=setting.margin_mode if setting is not None else MARGIN_MODE_ISOLATED,
                position_mode=(setting.position_mode if setting is not None else None) or POSITION_MODE_ONE_WAY,
            )
        if setting is None:
            setting = ContractUserSetting(
                user_id=user_id,
                market_id=market.id,
                leverage=market.default_leverage,
                margin_mode=MARGIN_MODE_ISOLATED,
                position_mode=POSITION_MODE_ONE_WAY,
            )
            session.add(setting)
            await session.flush()
        if not getattr(setting, "position_mode", None):
            setting.position_mode = POSITION_MODE_ONE_WAY
        return setting

    async def update_setting(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        *,
        leverage: Decimal,
        margin_mode: str,
        position_mode: str | None = None,
    ) -> ContractUserSetting:
        if leverage <= ZERO:
            raise ContractValidationError("leverage must be positive")
        if leverage > Decimal(market.max_leverage):
            raise ContractValidationError(f"leverage exceeds max_leverage {market.max_leverage}")
        if margin_mode != MARGIN_MODE_ISOLATED:
            raise ContractValidationError("only isolated margin is enabled in this MVP")
        if position_mode is not None and position_mode not in {POSITION_MODE_ONE_WAY, POSITION_MODE_HEDGE}:
            raise ContractValidationError("unsupported position_mode")
        live_orders = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            )
        )
        if int(live_orders or 0) > 0:
            raise ContractValidationError("cannot change leverage while contract orders are open")
        position = await self.get_position(session, user.id, market, create=False)
        if position is not None and Decimal(position.quantity) > ZERO:
            raise ContractValidationError("cannot change leverage while a position is open")
        setting = await self.get_setting(session, user.id, market)
        setting.leverage = leverage
        setting.margin_mode = margin_mode
        if position_mode is not None:
            setting.position_mode = position_mode
        await session.commit()
        return setting

    async def get_position(
        self,
        session: AsyncSession,
        user_id: int,
        market: Market,
        *,
        create: bool = True,
        side: str | None = None,
    ) -> ContractPosition | None:
        stmt = select(ContractPosition).where(
            ContractPosition.user_id == user_id,
            ContractPosition.market_id == market.id,
        )
        if side is not None:
            stmt = stmt.where(ContractPosition.side == side)
            position = await session.scalar(stmt)
        else:
            result = await session.execute(
                stmt.order_by(ContractPosition.quantity.desc(), ContractPosition.id.asc())
            )
            positions = list(result.scalars())
            position = next(
                (
                    item
                    for item in positions
                    if Decimal(item.quantity) > ZERO and item.side in {POSITION_SIDE_LONG, POSITION_SIDE_SHORT}
                ),
                None,
            )
            if position is None:
                position = next((item for item in positions if item.side == POSITION_SIDE_FLAT), None)
            if position is None and positions:
                position = positions[0]

        if position is None and create:
            account_run_id = await session.scalar(select(User.current_account_run_id).where(User.id == user_id))
            position = ContractPosition(
                user_id=user_id,
                market_id=market.id,
                side=side if side in {POSITION_SIDE_LONG, POSITION_SIDE_SHORT} else POSITION_SIDE_FLAT,
                quantity=ZERO,
                entry_price=ZERO,
                mark_price=self.mark_price(market),
                liquidation_price=ZERO,
                leverage=market.default_leverage,
                margin_mode=MARGIN_MODE_ISOLATED,
                isolated_margin=ZERO,
                maintenance_margin=ZERO,
                unrealized_pnl=ZERO,
                realized_pnl=ZERO,
                account_run_id=account_run_id,
            )
            session.add(position)
            await session.flush()
        return position

    async def is_hedge_position_mode(self, session: AsyncSession, user_id: int, market: Market) -> bool:
        if market.product_type != PRODUCT_TYPE_PERP:
            return False
        setting = await self.get_setting(session, user_id, market, persist=False)
        return setting.position_mode == POSITION_MODE_HEDGE

    async def is_hedge_market_maker(self, session: AsyncSession, user_id: int, market: Market) -> bool:
        return await self.is_hedge_position_mode(session, user_id, market)

    async def quote_validation_context(self, session: AsyncSession, user: User, market: Market) -> dict:
        """Load immutable per-QuoteSet risk inputs once, never once per level."""
        # Do not create a missing setting inside four concurrent QuoteSet
        # requests.  A maker quote can use the market default for this command;
        # the account-settings endpoint remains responsible for provisioning a
        # durable user setting.
        setting = await session.scalar(
            select(ContractUserSetting).where(
                ContractUserSetting.user_id == user.id,
                ContractUserSetting.market_id == market.id,
            )
        )
        tiers = await self.risk_tiers_for_market(session, market)
        return {
            "leverage": Decimal(setting.leverage) if setting is not None else Decimal(market.default_leverage),
            "hedge_mode": bool(setting is not None and str(setting.position_mode) == POSITION_MODE_HEDGE),
            "risk_tiers": tiers,
        }

    def mark_price(self, market: Market) -> Decimal:
        snapshot = self.runtime.contract_price_snapshots.get(market.symbol)
        if snapshot:
            mark = to_decimal(snapshot.get("mark_price"))
            if mark > ZERO:
                return quantize_scale(mark, market.price_precision)
        reference = self.runtime.engine.reference_price(market.symbol)
        if reference is not None and reference > ZERO:
            return quantize_scale(reference, market.price_precision)
        if market.reference_price is not None and Decimal(market.reference_price) > ZERO:
            return quantize_scale(market.reference_price, market.price_precision)
        return ZERO

    async def refresh_position(self, position: ContractPosition, market: Market, session: AsyncSession | None = None) -> None:
        qty = Decimal(position.quantity)
        mark = self.mark_price(market)
        position.updated_at = datetime.now(tz=UTC)
        position.mark_price = mark
        if qty <= ZERO or position.side == POSITION_SIDE_FLAT:
            position.side = POSITION_SIDE_FLAT
            position.quantity = ZERO
            position.entry_price = ZERO
            position.isolated_margin = ZERO
            position.maintenance_margin = ZERO
            position.unrealized_pnl = ZERO
            position.liquidation_price = ZERO
            return

        entry = Decimal(position.entry_price)
        notional = mark * qty
        risk_tier = await self.risk_tier_for_notional(session, market, notional)
        maintenance_margin = self.maintenance_margin_for_notional(notional, risk_tier)
        if position.side == POSITION_SIDE_LONG:
            unrealized = (mark - entry) * qty
            liquidation = ((entry * qty) - Decimal(position.isolated_margin) + maintenance_margin) / qty
        else:
            unrealized = (entry - mark) * qty
            liquidation = ((entry * qty) + Decimal(position.isolated_margin) - maintenance_margin) / qty
        position.unrealized_pnl = unrealized
        position.maintenance_margin = maintenance_margin
        position.liquidation_price = quantize_scale(max(liquidation, ZERO), market.price_precision)

    async def refresh_account(self, session: AsyncSession, account: ContractAccount) -> ContractAccount:
        rows = await session.execute(
            select(ContractPosition, Market)
            .join(Market, Market.id == ContractPosition.market_id)
            .where(
                ContractPosition.user_id == account.user_id,
                Market.margin_asset == account.margin_asset,
            )
        )
        unrealized = ZERO
        for position, market in rows.all():
            await self.refresh_position(position, market, session)
            unrealized += Decimal(position.unrealized_pnl)
        used_margin = Decimal(account.used_margin)
        if -CONTRACT_EPSILON < used_margin < CONTRACT_EPSILON:
            used_margin = ZERO
        account.used_margin = used_margin
        account.unrealized_pnl = unrealized
        account.available_margin = Decimal(account.wallet_balance) + unrealized - used_margin
        account.updated_at = datetime.now(tz=UTC)
        await session.flush()
        return account

    async def refresh_account_with_ledger(
        self,
        session: AsyncSession,
        account: ContractAccount,
        *,
        market: Market,
        change_type: str = "mark_to_market",
        note: str,
        created_at: datetime | None = None,
    ) -> ContractAccount:
        before = snapshot_contract_account(account)
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type=change_type,
            amount=ZERO,
            before=before,
            market_id=market.id,
            note=note,
            created_at=created_at or datetime.now(tz=UTC),
        )
        return account

    def _contract_maker_margin_ok(self, user_id: int, market_id: int) -> bool:
        """Per-fill resting-maker solvency check (Hyperliquid-style recheck)."""
        if (int(user_id), int(market_id)) in self._ladder_bindings:
            return True
        position = self.runtime.clearinghouse.position_snapshot(int(user_id), int(market_id))
        account = None
        for (uid, _asset), state in self.runtime.clearinghouse._contract.items():
            if int(uid) == int(user_id) and (account is None or Decimal(state.wallet_balance) > Decimal(account.wallet_balance)):
                account = state
        if account is None:
            return False
        equity = Decimal(account.wallet_balance) + Decimal(account.unrealized_pnl)
        maintenance = Decimal(position.maintenance_margin) if position is not None else ZERO
        return equity + Decimal("1e-8") >= maintenance

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
        now = now or datetime.now(tz=UTC)
        market = await self.get_market(session, payload.symbol)
        market_id = int(market.id)
        market_symbol = str(market.symbol)
        if market_symbol in self._ladder_financial_unknown:
            raise ContractValidationError("market has unresolved internal user settlement")
        self._normalize_payload(market, payload)
        order_id = next_order_id()
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self.contract_financial_keys(session, market, user.id)
        ):
            from app.services.sqlite_write_admission import acquire_sqlite_write_admission
            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
            await session.refresh(market)
            # Queries below must observe facts committed while the guard was waiting.
            self._expire_financial_reads(session)
            if reconcile_book:
                await self.reconcile_engine_book(session, market)
            existing_order = await self._load_live_client_order(session, user, market, payload.client_order_id)
            if existing_order is not None:
                response = {
                    "order": await self.serialize_order(session, existing_order, market.symbol),
                    "idempotent": True,
                }
                if not self._live_client_order_matches(existing_order, payload):
                    response["client_order_conflict"] = True
                return response
            if market.is_active and market.contract_trading_mode != CONTRACT_TRADING_MODE_PAUSED:
                await self.prepare_resting_fills(session, market, lambda: self._ordinary_preview(market, payload, user))
            try:
                reserve = await self.validate_order(session, user, market, payload)
            except ContractValidationError as exc:
                order = await self.create_order_record(
                    session,
                    user=user,
                    market=market,
                    payload=payload,
                    order_id=order_id,
                    status=ORDER_STATUS_REJECTED,
                    leverage=None,
                    reject_reason=str(exc),
                    created_at=now,
                )
                await session.commit()
                return {"order": await self.serialize_order(session, order, market.symbol)}

            account = await self.get_account(session, user.id, market.margin_asset or market.quote_asset)
            if reserve.amount > ZERO:
                before = snapshot_contract_account(account)
                account.used_margin = Decimal(account.used_margin) + reserve.amount
                await self.refresh_account(session, account)
                if Decimal(account.available_margin) < -CONTRACT_EPSILON:
                    raise ContractValidationError("insufficient available margin")
                await add_contract_ledger_entry(
                    session,
                    account,
                    change_type="margin_reserve",
                    amount=ZERO,
                    before=before,
                    market_id=market.id,
                    related_order_id=order_id,
                    note="contract_order_initial_margin_reserve",
                    created_at=now,
                )

            sequence_number = self.runtime.next_sequence(market.symbol)
            order = await self.create_order_record(
                session,
                user=user,
                market=market,
                payload=payload,
                order_id=order_id,
                status=ORDER_STATUS_NEW,
                leverage=reserve.leverage,
                reject_reason=None,
                created_at=now,
                sequence_number=sequence_number,
            )
            order.reserved_margin = reserve.amount
            await self.runtime.clearinghouse.refresh_contract_market(session, market.id)
            await self._preflight_user_makers(session, market, side=payload.side,
                quantity=Decimal(order.quantity), price=Decimal(order.price) if order.price is not None else None,
                margin_guard=True, taker_order=order, stp_context=self._stp_context(user))
            admission_changes = await self.rebalance_user_orders(session, market, user.id, now=now)
            if order.status == ORDER_STATUS_CANCELED:
                await session.commit()
                return {"order": await self.serialize_order(session, order, market.symbol), "trades": []}
            result = self.runtime.engine.process_order(
                symbol=market.symbol,
                order_id=order.order_id,
                user_id=user.id,
                side=payload.side,
                quantity=Decimal(order.quantity),
                created_at=now,
                limit_price=Decimal(order.price) if order.price is not None else None,
                can_rest=payload.type == ORDER_TYPE_LIMIT and payload.tif in {TIF_GTC, TIF_POST_ONLY},
                sequence_number=sequence_number,
                maker_guard=lambda order_id, maker_user_id: self._contract_maker_margin_ok(
                    maker_user_id, market.id
                ),
                **self._stp_context(user),
            )
            try:
                updated_orders, impacted_users, trade_payloads, total_notional = await self.apply_engine_fills(
                    session=session,
                    market=market,
                    taker_user=user,
                    taker_order=order,
                    result=result,
                    executed_at=now,
                )
            except Exception:
                # Engine fills precede SQL commit. Restore the committed book
                # when either settlement side, position update, ledger,
                # Accounting or Outbox write fails mid-fill.
                await session.rollback()
                if any(mid == market_id for _uid, mid in self._ladder_bindings):
                    self._ladder_financial_unknown[market_symbol] = order_id
                else:
                    await self.rebuild_engine_book_from_db(session, market_id, reason="contract_settlement_failed")
                raise
            updated_orders.update({o.order_id: o for o in admission_changes})
            order.notional = total_notional
            order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
            order.avg_price = (total_notional / Decimal(order.filled_quantity)) if Decimal(order.filled_quantity) > ZERO else None
            order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
            order.updated_at = now
            if Decimal(order.filled_quantity) == ZERO:
                order.status = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
            elif Decimal(order.remaining_quantity) == ZERO:
                order.status = ORDER_STATUS_FILLED
            elif result.placed_on_book:
                order.status = ORDER_STATUS_PARTIALLY_FILLED
            else:
                order.status = ORDER_STATUS_CANCELED
                order.canceled_at = now
            await self.release_leftover(session, market, order, reserve, now)
            await self.refresh_account_with_ledger(
                session,
                account,
                market=market,
                note="contract_place_final_mark_to_market",
                created_at=now,
            )
            await self.commit_or_rebuild_engine(session, market, reason="contract_place_commit_failed")
            await self.runtime.clearinghouse.refresh_contract_market(session, market.id)

        for changed_order in updated_orders.values():
            if (int(changed_order.user_id), int(market.id)) in self._ladder_bindings:
                self.remember_ladder_order(self._fast_order_snapshot(changed_order, market))
        self._fast_metrics["slow_place"] += 1
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            snap = self._fast_order_snapshot(order, market)
            self._fast_contract_orders[order.order_id] = snap
            self._fast_register_client_id(user.id, market.id, payload.client_order_id, order.order_id)
        elif payload.client_order_id:
            self._fast_unregister_client_id(user.id, market.id, payload.client_order_id, order.order_id)
        response = {
            "order": await self.serialize_order(session, order, market.symbol),
            "trades": trade_payloads,
        }
        if broadcast:
            await self.broadcast_order_flow(
                session,
                market.symbol,
                list(updated_orders.values()),
                impacted_users,
                result.changed_bids,
                result.changed_asks,
                trade_payloads,
            )
        else:
            response["_broadcast_flow"] = {
                "symbol": market.symbol,
                "orders": list(updated_orders.values()),
                "impacted_users": impacted_users,
                "changed_bids": result.changed_bids,
                "changed_asks": result.changed_asks,
                "trade_payloads": trade_payloads,
            }
        return response

    @staticmethod
    def _reserve_row(order, *, price=None, remaining=None):
        return SimpleNamespace(order_id=str(order.order_id), side=order.side,
            position_action=order.position_action, reduce_only=bool(order.reduce_only),
            remaining_quantity=Decimal(order.remaining_quantity if remaining is None else remaining),
            price=Decimal(price if price is not None else order.price or order.reference_price or ZERO),
            leverage=Decimal(order.leverage or 1), sequence_number=int(order.sequence_number or 0))

    async def _live_user_orders(self, session, market, user_id):
        return list((await session.scalars(select(Order).where(
            Order.user_id == user_id, Order.market_id == market.id, Order.product_type == PRODUCT_TYPE_PERP,
            Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]), Order.remaining_quantity > ZERO,
            Order.type == ORDER_TYPE_LIMIT, Order.tif.in_([TIF_GTC, TIF_POST_ONLY])
        ))).all())

    async def _net_order_reserve(self, session, market, user_id, payload, leverage, *, exclude=None):
        position = await self.get_position(session, user_id, market, create=False)
        orders = await self._live_user_orders(session, market, user_id)
        rows = [self._reserve_row(o) for o in orders if o.order_id != exclude]
        candidate_qty = Decimal(payload.quantity)
        candidate_price = Decimal(payload.price or self.mark_price(market))
        if getattr(payload, "type", None) == ORDER_TYPE_MARKET:
            fills = self.runtime.engine.ensure_market(market.symbol).preview_bbo_order(
                side=payload.side, quantity=candidate_qty,
                limit_price=Decimal("Infinity") if payload.side == SIDE_BUY else ZERO).fills
            candidate_qty = sum((f.quantity for f in fills), ZERO)
            candidate_price = max((f.price for f in fills), default=candidate_price)
        candidate = SimpleNamespace(order_id=exclude or "~candidate", side=payload.side,
            position_action=payload.position_action, reduce_only=bool(getattr(payload, "reduce_only", False)),
            remaining_quantity=candidate_qty, price=candidate_price,
            leverage=leverage, sequence_number=max((r.sequence_number for r in rows), default=0) + 1)
        rows.append(candidate)
        reserves = required_reserves(position.side if position else POSITION_SIDE_FLAT,
            self._position_qty_without_storage_dust(market, position.quantity) if position else ZERO, rows)
        return reserves[candidate.order_id]

    async def rebalance_user_orders(self, session, market, user_id, *, now=None, mutate_engine=True):
        """Reprice affected user's leaves; never scan the market book or touch strategy intent."""
        if await self.is_contract_ladder(session, user_id, market.id):
            return []
        if await self.is_hedge_market_maker(session, user_id, market):
            return []
        orders = await self._live_user_orders(session, market, user_id)
        if not orders:
            return []
        position = await self.get_position(session, user_id, market, create=False)
        account = await self.get_account(session, user_id, market.margin_asset or market.quote_asset)
        before = snapshot_contract_account(account)
        old = sum((self.reserved_margin_for_fill(o, Decimal(o.remaining_quantity), Decimal(o.price or o.reference_price or 0)) for o in orders), ZERO)
        account.used_margin = Decimal(account.used_margin) - old
        await self.refresh_account(session, account)
        capacity = max(Decimal(account.available_margin), ZERO)
        reserves = required_reserves(position.side if position else POSITION_SIDE_FLAT,
            self._position_qty_without_storage_dust(market, position.quantity) if position else ZERO,
            [self._reserve_row(o) for o in orders], available=capacity)
        changed = []
        for order in sorted(orders, key=lambda o: (not (o.position_action == POSITION_ACTION_CLOSE or o.reduce_only), int(o.sequence_number or 0), o.order_id)):
            reserve = reserves[order.order_id]
            if reserve is None or reserve > capacity + CONTRACT_EPSILON:
                order.status = ORDER_STATUS_CANCELED
                order.reject_reason = "remaining reduce-only capacity exhausted" if reserve is None else "remaining order margin insufficient"
                order.canceled_at = now or datetime.now(tz=UTC)
                order.updated_at = order.canceled_at
                order.version = int(order.version or 0) + 1
                order.reserved_margin = ZERO
                if mutate_engine:
                    self.runtime.engine.cancel_order(market.symbol, order.order_id)
                self._fast_contract_orders.pop(order.order_id, None)
                self._fast_unregister_client_id(user_id, market.id, order.client_order_id, order.order_id)
                changed.append(order)
            else:
                order.reserved_margin = reserve
                capacity -= reserve
                account.used_margin = Decimal(account.used_margin) + reserve
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(session, account, change_type="margin_settle", amount=ZERO,
            before=before, market_id=market.id, note="contract_remaining_risk_rebalance", created_at=now)
        return changed

    async def validate_order(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        payload,
        *,
        fast: bool = False,
        quote_context: dict | None = None,
    ) -> ContractReservePlan:
        restricted = (market.contract_trading_mode == CONTRACT_TRADING_MODE_REDUCE_ONLY or
            (market.is_listed and market.paper_status in {"REDUCE_ONLY", "DELISTING"}))
        if restricted and payload.position_action == POSITION_ACTION_OPEN:
            if not await self.is_hedge_position_mode(session, user.id, market):
                current = await self.get_position(session, user.id, market, create=False)
                if current is None or Decimal(current.quantity) <= ZERO or current.side == self.order_position_side(payload.side, POSITION_ACTION_OPEN):
                    raise ContractValidationError("contract market is reduce-only")
                payload.position_action = POSITION_ACTION_CLOSE
                payload.reduce_only = True
        if not market.is_active:
            raise ContractValidationError("market is inactive")
        if market.is_listed:
            # 对外白标市场按 PRD 上币状态机校验；实验市场只校验 is_active。
            paper_status = str(getattr(market, "paper_status", "TRADING"))
            paper_quote = str(getattr(payload, "client_order_id", "") or "").startswith("paperq-")
            if paper_status not in {"TRADING", "REDUCE_ONLY", "DELISTING"} and not (
                paper_status == "PRE_OPEN" and user.role == ROLE_BOT and paper_quote
            ):
                raise ContractValidationError(f"paper market is {paper_status}; new contract orders are disabled")
            if paper_status in {"REDUCE_ONLY", "DELISTING"} and payload.position_action != POSITION_ACTION_CLOSE:
                raise ContractValidationError("paper market is reduce-only")
        trading_mode = market.contract_trading_mode or "normal"
        if trading_mode == CONTRACT_TRADING_MODE_PAUSED:
            raise ContractValidationError("contract market is paused")
        if trading_mode == CONTRACT_TRADING_MODE_REDUCE_ONLY and payload.position_action == POSITION_ACTION_OPEN:
            raise ContractValidationError("contract market is reduce-only")
        quantity = self._normalize_qty(market, payload.quantity)
        if quantity <= ZERO:
            raise ContractValidationError("quantity must be positive")
        if not is_step_aligned(quantity, quantize_scale(market.qty_step, market.qty_precision)):
            raise ContractValidationError("quantity does not match qty_step")
        if quantity < Decimal(market.min_qty):
            raise ContractValidationError("quantity below min_qty")
        order_notional = ZERO
        if payload.type == ORDER_TYPE_LIMIT:
            price = self._normalize_price(market, payload.price)
            if price is None or price <= ZERO:
                raise ContractValidationError("price must be positive")
            if not is_step_aligned(price, quantize_scale(market.price_tick, market.price_precision)):
                raise ContractValidationError("price does not match tick")
            order_notional = price * quantity
            if order_notional < Decimal(market.min_notional) and not await min_notional_exempt(session, user, market, payload):
                raise ContractValidationError("notional below min_notional")
            if getattr(payload, "tif", None) == TIF_POST_ONLY:
                book, _, _ = self.runtime.orderbook_snapshot_unlocked(market.symbol, depth=1)
                best_ask = Decimal(book["asks"][0][0]) if book.get("asks") else None
                best_bid = Decimal(book["bids"][0][0]) if book.get("bids") else None
                if (payload.side == SIDE_BUY and best_ask is not None and price >= best_ask) or (
                    payload.side == SIDE_SELL and best_bid is not None and price <= best_bid
                ):
                    raise ContractValidationError("post_only order would take liquidity")
        else:
            filled_qty, notional = self.runtime.engine.simulate_cost(market.symbol, payload.side, quantity)
            if filled_qty <= ZERO:
                raise ContractValidationError("no liquidity on book")
            if notional < Decimal(market.min_notional):
                raise ContractValidationError("notional below min_notional")
            order_notional = notional

        if await self.is_contract_ladder(session, user.id, market.id):
            if payload.type != ORDER_TYPE_LIMIT or payload.tif != TIF_GTC or payload.position_action != POSITION_ACTION_OPEN or payload.reduce_only:
                raise ContractValidationError("internal ladder only supports ordinary open limit GTC")
            return ContractReservePlan(amount=ZERO, leverage=Decimal("1"))
        setting = None if quote_context else await self.get_setting(session, user.id, market, persist=not fast)
        requested_leverage = getattr(payload, "leverage", None)
        default_leverage = (
            quote_context.get("leverage")
            if quote_context is not None
            else setting.leverage
        )
        leverage = Decimal(requested_leverage) if requested_leverage is not None else Decimal(default_leverage)
        if leverage > Decimal(market.max_leverage):
            raise ContractValidationError(f"leverage exceeds max_leverage {market.max_leverage}")

        target_side = self.order_position_side(payload.side, payload.position_action)
        hedge_mode = (
            bool(quote_context.get("hedge_mode"))
            if quote_context is not None
            else setting.position_mode == POSITION_MODE_HEDGE
        )

        if payload.position_action == POSITION_ACTION_CLOSE:
            await self.validate_close_quantity(
                session,
                user.id,
                market,
                payload,
                target_side=target_side,
                hedge_mode=hedge_mode,
                fast=fast,
            )
            return ContractReservePlan(amount=ZERO, leverage=leverage)

        if fast:
            position_state = self.runtime.clearinghouse.position_snapshot(user.id, market.id)
            position = None
            if position_state is not None and position_state.quantity > ZERO:
                if hedge_mode or position_state.side == target_side:
                    position = SimpleNamespace(
                        quantity=position_state.quantity,
                        side=position_state.side,
                        mark_price=position_state.mark_price,
                    )
        else:
            position = await self.get_position(
                session,
                user.id,
                market,
                create=False,
                side=target_side if hedge_mode else None,
            )

        target_notional = await self.target_open_notional(session, market, position, order_notional, fast=fast)
        if not hedge_mode and position is not None and position.side != target_side:
            current_qty = self._position_qty_without_storage_dust(market, position.quantity)
            prospective = max(ZERO, quantity - current_qty)
            target_notional = prospective * (Decimal(payload.price) if payload.price is not None else self.mark_price(market))
        if quote_context is not None and quote_context.get("risk_tiers"):
            tiers = list(quote_context["risk_tiers"])
            risk_tier = tiers[-1]
            target = max(to_decimal(target_notional), ZERO)
            for tier in tiers:
                floor = to_decimal(tier.notional_floor)
                cap = to_decimal(tier.notional_cap) if tier.notional_cap is not None else None
                if target >= floor and (cap is None or target < cap):
                    risk_tier = tier
                    break
        else:
            risk_tier = await self.risk_tier_for_notional(session, market, target_notional)
        tier_max_leverage = min(Decimal(market.max_leverage), Decimal(risk_tier.max_leverage))
        if leverage > tier_max_leverage:
            raise ContractValidationError(
                f"leverage exceeds risk tier {risk_tier.tier} max_leverage {decimal_to_str(tier_max_leverage)}"
            )

        margin_asset = market.margin_asset or market.quote_asset
        reserve = self.reserve_for_payload(market, payload, leverage)
        if not hedge_mode and not fast:
            reserve.amount = await self._net_order_reserve(session, market, user.id, payload, leverage)
        if fast:
            available_margin = self.runtime.clearinghouse.contract_available(user.id, margin_asset)
            if available_margin < reserve.amount:
                raise ContractValidationError("insufficient available margin")
        else:
            account = await self.get_account(session, user.id, margin_asset)
            await self.refresh_account_with_ledger(
                session,
                account,
                market=market,
                note="contract_order_validation_mark_to_market",
            )
            if Decimal(account.available_margin) < reserve.amount:
                raise ContractValidationError("insufficient available margin")
        return ContractReservePlan(amount=reserve.amount, leverage=leverage)

    async def target_open_notional(
        self,
        session: AsyncSession,
        market: Market,
        position: ContractPosition | None,
        order_notional: Decimal,
        *,
        fast: bool = False,
    ) -> Decimal:
        if position is None or Decimal(position.quantity) <= ZERO or position.side == POSITION_SIDE_FLAT:
            return order_notional
        if fast:
            mark = Decimal(position.mark_price) if getattr(position, "mark_price", None) is not None else self.mark_price(market)
            current_notional = mark * Decimal(position.quantity)
        else:
            await self.refresh_position(position, market, session)
            current_notional = Decimal(position.mark_price) * Decimal(position.quantity)
        return max(current_notional, ZERO) + order_notional

    async def validate_close_quantity(
        self,
        session: AsyncSession,
        user_id: int,
        market: Market,
        payload,
        *,
        target_side: str | None = None,
        hedge_mode: bool = False,
        fast: bool = False,
        exclude_order_id: str | None = None,
    ) -> None:
        target_side = target_side or self.order_position_side(payload.side, payload.position_action)
        if fast:
            position_state = self.runtime.clearinghouse.position_snapshot(user_id, market.id)
            if position_state is None or position_state.side != target_side or position_state.quantity <= ZERO:
                raise ContractValidationError("no matching position to close")
            position_qty = self._position_qty_without_storage_dust(market, position_state.quantity)
            open_reduce_qty = ZERO
            for order_id, snap in self._fast_contract_orders.items():
                if exclude_order_id and str(order_id) == str(exclude_order_id):
                    continue
                if (
                    int(snap.get("user_id") or -1) != int(user_id)
                    or int(snap.get("market_id") or -1) != int(market.id)
                    or str(snap.get("status") or "") not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                    or str(snap.get("position_action") or "") != POSITION_ACTION_CLOSE
                    or not bool(snap.get("reduce_only"))
                    or (hedge_mode and str(snap.get("side") or "") != str(payload.side))
                ):
                    continue
                open_reduce_qty += max(Decimal(snap.get("remaining_quantity") or ZERO), ZERO)
        else:
            position = await self.get_position(
                session,
                user_id,
                market,
                create=False,
                side=target_side if hedge_mode else None,
            )
            if position is None or position.side != target_side or Decimal(position.quantity) <= ZERO:
                raise ContractValidationError("no matching position to close")
            position_qty = self._position_qty_without_storage_dust(market, position.quantity)
            reduce_stmt = select(func.coalesce(func.sum(Order.remaining_quantity), 0)).where(
                Order.user_id == user_id,
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.position_action == POSITION_ACTION_CLOSE,
                Order.reduce_only.is_(True),
            )
            if hedge_mode:
                reduce_stmt = reduce_stmt.where(Order.side == payload.side)
            if exclude_order_id:
                reduce_stmt = reduce_stmt.where(Order.order_id != str(exclude_order_id))
            open_reduce_qty = await session.scalar(reduce_stmt)
        available = position_qty - self._position_qty_without_storage_dust(market, open_reduce_qty or 0)
        if payload.quantity > available:
            raise ContractValidationError("close quantity exceeds available position")

    def reserve_for_payload(self, market: Market, payload, leverage: Decimal) -> ContractReservePlan:
        quantity = Decimal(payload.quantity)
        if payload.position_action != POSITION_ACTION_OPEN:
            return ContractReservePlan(amount=ZERO, leverage=leverage)
        if payload.type == ORDER_TYPE_LIMIT and payload.price is not None:
            return ContractReservePlan(amount=Decimal(payload.price) * quantity / leverage, leverage=leverage)
        _, notional = self.runtime.engine.simulate_cost(market.symbol, payload.side, quantity)
        return ContractReservePlan(amount=notional / leverage, leverage=leverage)

    @staticmethod
    def order_position_side(order_side: str, action: str | None) -> str:
        if action == POSITION_ACTION_CLOSE:
            return POSITION_SIDE_LONG if order_side == SIDE_SELL else POSITION_SIDE_SHORT
        return POSITION_SIDE_LONG if order_side == SIDE_BUY else POSITION_SIDE_SHORT

    async def create_order_record(
        self,
        session: AsyncSession,
        *,
        user: User,
        market: Market,
        payload,
        order_id: str,
        status: str,
        leverage: Decimal | None,
        reject_reason: str | None,
        created_at: datetime,
        sequence_number: int = 0,
    ) -> Order:
        order = Order(
            order_id=order_id,
            client_order_id=payload.client_order_id,
            user_id=user.id,
            account_run_id=user.current_account_run_id,
            market_id=market.id,
            product_type=PRODUCT_TYPE_PERP,
            side=payload.side,
            position_action=payload.position_action,
            reduce_only=payload.reduce_only,
            leverage=leverage,
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
            reference_price=self.mark_price(market),
            protection_bps=None,
            max_price=None,
            min_price=None,
            reject_reason=reject_reason,
            created_at=created_at,
            updated_at=created_at,
        )
        session.add(order)
        await session.flush()
        return order

    async def load_orders_map(self, session: AsyncSession, order_ids: set[str]) -> dict[str, Order]:
        if not order_ids:
            return {}
        rows = await session.execute(select(Order).where(Order.order_id.in_(order_ids)))
        return {order.order_id: order for order in rows.scalars()}

    async def apply_engine_fills(
        self,
        *,
        session: AsyncSession,
        market: Market,
        taker_user: User,
        taker_order: Order,
        result,
        executed_at: datetime,
        ingest: bool = True,
    ) -> tuple[dict[str, Order], set[int], list[dict], Decimal]:
        maker_order_ids = {fill.maker_order_id for fill in result.fills}
        maker_orders = await self.load_orders_map(session, maker_order_ids)
        for maker_id in maker_order_ids:
            snap = self._fast_contract_orders.get(maker_id)
            if snap is not None and await self.is_contract_ladder(session, int(snap["user_id"]), market.id):
                maker_orders[maker_id] = await self.ensure_ladder_order_anchor(session, market, snap)
        # Internal LADDER anchors were materialized above in this transaction.
        # Waiting on a separate SQL writer here would wait on our own write lock.
        if len(maker_orders) < len(maker_order_ids):
            missing = sorted(maker_order_ids - set(maker_orders))
            raise ContractValidationError(f"maker orders not persisted: {missing}")
        impacted_users = {taker_user.id}
        updated_orders = {taker_order.order_id: taker_order}
        trade_payloads: list[dict] = []
        total_notional = ZERO
        taker_fee_rate = await self.get_fee_rate(session, taker_user.id, market.id, taker=True, market=market)
        maker_fee_rates: dict[int, Decimal] = {}

        for fill in result.fills:
            maker_order = maker_orders[fill.maker_order_id]
            if maker_order.user_id not in maker_fee_rates:
                maker_fee_rates[maker_order.user_id] = await self.get_fee_rate(session, maker_order.user_id, market.id, taker=False, market=market)
            trade_id = next_trade_id()
            trade = await self.apply_trade(
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
                trade_id=trade_id,
            )
            total_notional += Decimal(trade.quote_amount)
            impacted_users.add(maker_order.user_id)
            updated_orders[maker_order.order_id] = maker_order
            if ingest:
                chart_trade = dict(symbol=market.symbol, price=Decimal(trade.price), quantity=Decimal(trade.quantity),
                    side=taker_order.side, ts=executed_at, trade_id=trade.trade_id,
                    price_scale=market.price_precision, qty_scale=market.qty_precision, source=trade.source)
                if platform_durable_contract():
                    transaction = session.sync_session.get_transaction()
                    previous = session.info.get("contract_chart_batch")
                    if previous is None or previous[0] is not transaction:
                        session.info["contract_chart_batch"] = (transaction, [])
                    session.info["contract_chart_batch"][1].append(chart_trade)
                else:
                    self.runtime.market_data.ingest_trade(**chart_trade)
            await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "1m")
            await self.runtime.market_data.persist_kline(session, market.id, market.symbol, "5m")
            trade_payloads.append(await self.serialize_trade(trade, market.symbol, market))
        if result.fills:
            for uid in impacted_users:
                for changed in await self.rebalance_user_orders(session, market, uid, now=executed_at, mutate_engine=ingest):
                    updated_orders[changed.order_id] = changed
        return updated_orders, impacted_users, trade_payloads, total_notional

    async def get_fee_rate(self, session: AsyncSession, user_id: int, market_id: int, *, taker: bool, market: Market) -> Decimal:
        if await self.is_contract_ladder(session, user_id, market_id):
            return ZERO
        profile = await session.scalar(select(FeeProfile).where(FeeProfile.user_id == user_id, FeeProfile.market_id == market_id))
        if profile is None:
            return Decimal(market.default_taker_fee_rate if taker else market.default_maker_fee_rate)
        return Decimal(profile.taker_fee_rate if taker else profile.maker_fee_rate)

    @staticmethod
    def _trade_source_from_orders(taker_order: Order, maker_order: Order) -> str | None:
        client_order_ids = [
            value
            for value in (taker_order.client_order_id, maker_order.client_order_id)
            if value
        ]
        if any(str(value).startswith(("flowv2-", "flow-", "flowioc-", "perpflow-")) or "-flow-" in str(value) for value in client_order_ids):
            return "flow"
        if any(str(value).startswith(("mmv2-", "repairv2-", "perpmm-", "perpresearch-")) for value in client_order_ids):
            return "bot"
        return None

    async def trade_source(
        self,
        session: AsyncSession,
        taker_user: User,
        maker_user_id: int,
        *,
        taker_order: Order | None = None,
        maker_order: Order | None = None,
    ) -> str:
        if taker_order is not None and maker_order is not None:
            source = self._trade_source_from_orders(taker_order, maker_order)
            if source:
                return source
        maker_role = await session.scalar(select(User.role).where(User.id == maker_user_id))
        if taker_user.role == ROLE_BOT or maker_role == ROLE_BOT:
            return "bot"
        return "user"

    async def apply_trade(
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
        trade_id: str | None = None,
    ) -> Trade:
        trade_id = trade_id or next_trade_id()
        quote_amount = price * quantity
        maker_user = await session.scalar(select(User).where(User.id == maker_order.user_id))
        maker_fee = quote_amount * maker_fee_rate
        taker_fee = quote_amount * taker_fee_rate
        maker_realized = await self.settle_side(
            session=session,
            market=market,
            user_id=maker_order.user_id,
            order=maker_order,
            quantity=quantity,
            price=price,
            fee=maker_fee,
            executed_at=executed_at,
            related_trade_id=trade_id,
        )
        taker_realized = await self.settle_side(
            session=session,
            market=market,
            user_id=taker_user.id,
            order=taker_order,
            quantity=quantity,
            price=price,
            fee=taker_fee,
            executed_at=executed_at,
            related_trade_id=trade_id,
        )
        trade = Trade(
            trade_id=trade_id,
            business_key=(
                f"PERP:{market.id}:{taker_order.order_id}:{int(taker_order.version or 0)}:"
                f"{maker_order.order_id}:{int(maker_order.version or 0)}"
            ),
            market_id=market.id,
            product_type=PRODUCT_TYPE_PERP,
            price=price,
            quantity=quantity,
            quote_amount=quote_amount,
            taker_order_id=taker_order.order_id,
            maker_order_id=maker_order.order_id,
            taker_position_action=getattr(taker_order, "_last_fill_action", taker_order.position_action),
            maker_position_action=getattr(maker_order, "_last_fill_action", maker_order.position_action),
            taker_realized_pnl=taker_realized,
            maker_realized_pnl=maker_realized,
            taker_user_id=taker_user.id,
            maker_user_id=maker_order.user_id,
            account_run_id=taker_user.current_account_run_id,
            taker_account_run_id=taker_user.current_account_run_id,
            maker_account_run_id=getattr(maker_user, "current_account_run_id", None),
            global_run_id=getattr(self.runtime, "paper_global_run_id", None),
            taker_side=taker_order.side,
            source=await self.trade_source(
                session,
                taker_user,
                maker_order.user_id,
                taker_order=taker_order,
                maker_order=maker_order,
            ),
            maker_fee=maker_fee,
            taker_fee=taker_fee,
            fee_asset_maker=market.margin_asset or market.quote_asset,
            fee_asset_taker=market.margin_asset or market.quote_asset,
            executed_at=executed_at,
        )
        session.add(trade)

        for current in (taker_order, maker_order):
            current.filled_quantity = self._normalize_qty(market, Decimal(current.filled_quantity) + quantity)
            current.remaining_quantity = self._normalize_qty(market, Decimal(current.remaining_quantity) - quantity)
            current.notional = Decimal(current.notional or ZERO) + quote_amount
            current.avg_price = Decimal(current.notional) / Decimal(current.filled_quantity)
            current.updated_at = executed_at
            current.version = int(current.version or 0) + 1
            if Decimal(current.remaining_quantity) <= ZERO:
                current.remaining_quantity = ZERO
                current.status = ORDER_STATUS_FILLED
            else:
                current.status = ORDER_STATUS_PARTIALLY_FILLED
        await session.flush()
        await enqueue_financial_outbox(
            session,
            event_type="trade_committed",
            account_domain="contract",
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

    async def settle_side(
        self,
        *,
        session: AsyncSession,
        market: Market,
        user_id: int,
        order: Order,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        executed_at: datetime,
        related_trade_id: str | None = None,
    ) -> Decimal:
        if await self.is_contract_ladder(session, user_id, market.id):
            return ZERO
        account = await self.get_account(session, user_id, market.margin_asset or market.quote_asset)
        target_side = self.order_position_side(order.side, order.position_action or POSITION_ACTION_OPEN)
        hedge_mode = await self.is_hedge_market_maker(session, user_id, market)
        position = await self.get_position(
            session,
            user_id,
            market,
            create=order.position_action == POSITION_ACTION_OPEN,
            side=target_side if hedge_mode else None,
        )
        if position is None:
            raise ContractValidationError("no matching position to close")
        leverage = Decimal(order.leverage or market.default_leverage)
        try:
            split = split_fill(side=order.side, action=order.position_action, reduce_only=bool(order.reduce_only),
                hedge=hedge_mode, position_side=position.side,
                position_qty=self._position_qty_without_storage_dust(market, position.quantity), quantity=quantity)
        except ValueError as exc:
            raise ContractValidationError(str(exc)) from exc
        order._last_fill_action = split.action
        realized = ZERO
        released_margin = ZERO
        position_deleted = False
        before = snapshot_contract_account(account)
        reserved = self.reserved_margin_for_fill(order, quantity, price)
        if order.reserved_margin is not None:
            order.reserved_margin = max(ZERO, Decimal(order.reserved_margin) - reserved)
        if split.close:
            realized, released_margin = await self.apply_close_position(position, market, order, split.close, price)
        added_margin = price * split.opened / leverage
        if split.opened:
            await self.apply_open_position(position, market, order, split.opened, price, added_margin, leverage)
        account.wallet_balance = Decimal(account.wallet_balance) + realized
        account.used_margin = Decimal(account.used_margin) - released_margin + added_margin - reserved
        account.realized_pnl = Decimal(account.realized_pnl) + realized
        if hedge_mode and Decimal(position.quantity) <= ZERO:
            await session.delete(position)
            position_deleted = True
        if not position_deleted:
            await self.refresh_position(position, market, session)
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session, account, change_type="position_close" if split.close else "margin_settle",
            amount=realized, before=before, market_id=market.id,
            related_order_id=order.order_id, related_trade_id=related_trade_id,
            note=f"contract_{split.action}_settle", created_at=executed_at,
        )

        before_fee = snapshot_contract_account(account)
        account.wallet_balance = Decimal(account.wallet_balance) - fee
        account.total_fees = Decimal(account.total_fees) + fee
        account.updated_at = executed_at
        if not position_deleted:
            await self.refresh_position(position, market, session)
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="trade_fee",
            amount=-fee,
            before=before_fee,
            market_id=market.id,
            related_order_id=order.order_id,
            related_trade_id=related_trade_id,
            note="contract_trade_fee",
            created_at=executed_at,
        )
        return realized

    @staticmethod
    def reserved_margin_for_fill(order: Order, quantity: Decimal, price: Decimal) -> Decimal:
        stored = getattr(order, "reserved_margin", None)
        if stored is not None:
            remaining = Decimal(order.remaining_quantity)
            return Decimal(stored) * min(quantity, remaining) / remaining if remaining > ZERO else ZERO
        if order.position_action != POSITION_ACTION_OPEN:
            return ZERO
        reserve_price = Decimal(order.price) if order.type == ORDER_TYPE_LIMIT and order.price is not None else price
        return reserve_price * quantity / Decimal(order.leverage or 1)

    async def apply_open_position(
        self,
        position: ContractPosition,
        market: Market,
        order: Order,
        quantity: Decimal,
        price: Decimal,
        added_margin: Decimal,
        leverage: Decimal,
    ) -> None:
        target_side = self.order_position_side(order.side, POSITION_ACTION_OPEN)
        if Decimal(position.quantity) <= ZERO or position.side == POSITION_SIDE_FLAT:
            position.side = target_side
            position.quantity = quantity
            position.entry_price = price
            position.isolated_margin = added_margin
        elif position.side == target_side:
            old_qty = Decimal(position.quantity)
            new_qty = old_qty + quantity
            position.entry_price = ((Decimal(position.entry_price) * old_qty) + (price * quantity)) / new_qty
            position.quantity = new_qty
            position.isolated_margin = Decimal(position.isolated_margin) + added_margin
        else:
            raise ContractValidationError("opposite position cannot be opened in one-way mode")
        position.leverage = leverage
        position.margin_mode = MARGIN_MODE_ISOLATED
        await self.refresh_position(position, market)

    async def apply_close_position(
        self,
        position: ContractPosition,
        market: Market,
        order: Order,
        quantity: Decimal,
        price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        target_side = self.order_position_side(order.side, POSITION_ACTION_CLOSE)
        old_qty = self._position_qty_without_storage_dust(market, position.quantity)
        if position.side != target_side or old_qty < quantity:
            raise ContractValidationError("close quantity exceeds current position")
        close_ratio = quantity / old_qty
        if position.side == POSITION_SIDE_LONG:
            realized = (price - Decimal(position.entry_price)) * quantity
        else:
            realized = (Decimal(position.entry_price) - price) * quantity
        released_margin = Decimal(position.isolated_margin) * close_ratio
        position.quantity = old_qty - quantity
        position.isolated_margin = Decimal(position.isolated_margin) - released_margin
        position.realized_pnl = Decimal(position.realized_pnl) + realized
        if Decimal(position.quantity) <= CONTRACT_EPSILON:
            position.side = POSITION_SIDE_FLAT
            position.quantity = ZERO
            position.entry_price = ZERO
            position.isolated_margin = ZERO
        await self.refresh_position(position, market)
        return realized, released_margin

    async def liquidate_position(
        self,
        session: AsyncSession,
        *,
        market: Market,
        position: ContractPosition,
        account: ContractAccount,
        now: datetime,
        reason: str = "mark_price_liquidation",
    ) -> ContractLiquidationEvent | None:
        await self.refresh_position(position, market, session)
        risk = self.position_risk(position, market)
        if risk["risk_status"] != "liquidation_due":
            return None
        qty = Decimal(position.quantity)
        if qty <= ZERO or position.side not in {POSITION_SIDE_LONG, POSITION_SIDE_SHORT}:
            return None

        position_side = position.side
        entry_price = Decimal(position.entry_price)
        mark_price = Decimal(position.mark_price)
        liquidation_price = Decimal(position.liquidation_price)
        released_margin = Decimal(position.isolated_margin)
        maintenance_margin = Decimal(position.maintenance_margin)
        margin_buffer = to_decimal(risk["margin_buffer"])
        bankruptcy_price = ZERO
        if qty > ZERO:
            if position_side == POSITION_SIDE_LONG:
                bankruptcy_price = max(entry_price - (released_margin / qty), ZERO)
            else:
                bankruptcy_price = entry_price + (released_margin / qty)
        if position_side == POSITION_SIDE_LONG:
            realized = (mark_price - entry_price) * qty
        else:
            realized = (entry_price - mark_price) * qty

        before = snapshot_contract_account(account)
        event_id = next_liquidation_id()
        wallet_after_realized = Decimal(account.wallet_balance) + realized
        bad_debt = max(-wallet_after_realized, ZERO)
        insurance_covered = ZERO
        residual_bad_debt = ZERO
        if bad_debt > ZERO:
            insurance_covered, residual_bad_debt = await cover_contract_bad_debt(
                session,
                margin_asset=account.margin_asset,
                bad_debt=bad_debt,
                user_id=position.user_id,
                market_id=market.id,
                related_liquidation_event_id=event_id,
                note="liquidation_bad_debt",
                created_at=now,
            )
        position.realized_pnl = Decimal(position.realized_pnl) + realized
        hedge_mode = await self.is_hedge_market_maker(session, position.user_id, market)
        if hedge_mode:
            await session.delete(position)
        else:
            position.side = POSITION_SIDE_FLAT
            position.quantity = ZERO
            position.entry_price = ZERO
            position.isolated_margin = ZERO
            position.maintenance_margin = ZERO
            position.unrealized_pnl = ZERO
            position.liquidation_price = ZERO
            position.mark_price = mark_price
            position.updated_at = now

        account.wallet_balance = max(wallet_after_realized + insurance_covered, ZERO)
        account.used_margin = max(Decimal(account.used_margin) - released_margin, ZERO)
        account.realized_pnl = Decimal(account.realized_pnl) + realized
        account.updated_at = now
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="liquidation",
            amount=realized,
            before=before,
            market_id=market.id,
            related_event_id=event_id,
            note=reason,
            created_at=now,
        )

        event = ContractLiquidationEvent(
            event_id=event_id,
            user_id=position.user_id,
            market_id=market.id,
            position_side=position_side,
            quantity=qty,
            entry_price=entry_price,
            mark_price=mark_price,
            liquidation_price=liquidation_price,
            bankruptcy_price=bankruptcy_price,
            realized_pnl=realized,
            released_margin=released_margin,
            insurance_covered=insurance_covered,
            residual_bad_debt=residual_bad_debt,
            adl_covered=ZERO,
            adl_residual=residual_bad_debt,
            adl_status="pending" if residual_bad_debt > ZERO else "not_required",
            maintenance_margin=maintenance_margin,
            margin_buffer=margin_buffer,
            risk_status=risk["risk_status"],
            reason=reason,
            liquidated_at=now,
        )
        session.add(event)
        await session.flush()
        return event

    async def release_leftover(
        self,
        session: AsyncSession,
        market: Market,
        order: Order,
        reserve: ContractReservePlan,
        now: datetime,
    ) -> None:
        if order.position_action != POSITION_ACTION_OPEN:
            return
        remaining = Decimal(order.remaining_quantity)
        if remaining <= ZERO:
            return
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED} and order.type == ORDER_TYPE_LIMIT and order.tif in {TIF_GTC, TIF_POST_ONLY}:
            return
        release = self.reserved_margin_for_fill(order, remaining, Decimal(order.price or order.reference_price or 0))
        account = await self.get_account(session, order.user_id, market.margin_asset or market.quote_asset)
        before = snapshot_contract_account(account)
        account.used_margin = Decimal(account.used_margin) - min(max(release, ZERO), Decimal(account.used_margin))
        if order.reserved_margin is not None:
            order.reserved_margin = ZERO
        account.updated_at = now
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="margin_release",
            amount=ZERO,
            before=before,
            market_id=market.id,
            related_order_id=order.order_id,
            note="contract_order_leftover_margin_release",
            created_at=now,
        )

    async def cancel_order(self, session: AsyncSession, user: User, order_id: str, *, admin_override: bool = False) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        row = await session.execute(
            select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
        )
        record = row.first()
        if record is None:
            raise ContractValidationError("order not found")
        order, market = record
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ContractValidationError("order is not a contract order")
        if not admin_override and order.user_id != user.id:
            raise ContractValidationError("cannot cancel others order")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            raise ContractValidationError("order is not cancelable")
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self.contract_financial_keys(session, market, order.user_id)
        ):
            # The initial read preceded the market lock. Refresh after admission:
            # a fill/cancel may have completed while this request was waiting.
            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
            await session.refresh(order)
            if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                raise ContractValidationError("order is not cancelable")
            side_name, _, changes = self.runtime.engine.cancel_order(market.symbol, order.order_id)
            now = datetime.now(tz=UTC)
            if order.position_action == POSITION_ACTION_OPEN:
                release = self.reserved_margin_for_fill(order, Decimal(order.remaining_quantity), Decimal(order.price or order.reference_price or 0))
                account = await self.get_account(session, order.user_id, market.margin_asset or market.quote_asset)
                before = snapshot_contract_account(account)
                account.used_margin = Decimal(account.used_margin) - min(max(release, ZERO), Decimal(account.used_margin))
                await self.refresh_account(session, account)
                await add_contract_ledger_entry(
                    session,
                    account,
                    change_type="margin_release",
                    amount=ZERO,
                    before=before,
                    market_id=market.id,
                    related_order_id=order.order_id,
                    note="contract_order_cancel_margin_release",
                    created_at=now,
                )
            order.status = ORDER_STATUS_CANCELED
            order.canceled_at = now
            order.updated_at = now
            order.version = int(order.version or 0) + 1
            order.reserved_margin = ZERO
            await self.rebalance_user_orders(session, market, order.user_id, now=now)
            await self.commit_or_rebuild_engine(session, market, reason="contract_cancel_commit_failed")
        changed_bids = changes if side_name == SIDE_BUY else []
        changed_asks = changes if side_name == SIDE_SELL else []
        self._fast_metrics["slow_cancel"] += 1
        self._fast_contract_orders.pop(order.order_id, None)
        self._fast_unregister_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        await self.broadcast_order_flow(session, market.symbol, [order], {order.user_id}, changed_bids, changed_asks, [])
        return {"order": await self.serialize_order(session, order, market.symbol)}

    async def _adjust_contract_reserve(
        self,
        session: AsyncSession,
        market: Market,
        *,
        user_id: int,
        old_price: Decimal,
        old_remaining: Decimal,
        new_price: Decimal,
        new_remaining: Decimal,
        leverage: Decimal,
        related_order_id: str,
        now: datetime,
        fast: bool = False,
        client_order_id: str | None = None,
    ) -> None:
        row = None if fast else await session.scalar(select(Order).where(Order.order_id == related_order_id))
        if row is not None and (row.position_action == POSITION_ACTION_CLOSE or row.reduce_only):
            return
        if row is not None and row.reserved_margin is not None:
            desired = await self._net_order_reserve(session, market, user_id,
                SimpleNamespace(side=row.side, position_action=row.position_action, reduce_only=row.reduce_only,
                    quantity=new_remaining, price=new_price), leverage, exclude=row.order_id)
            old_reserve = Decimal(row.reserved_margin)
            account = await self.get_account(session, user_id, market.margin_asset or market.quote_asset)
            await self.refresh_account(session, account)
            delta = desired - old_reserve
            if delta > Decimal(account.available_margin) + CONTRACT_EPSILON:
                raise ContractValidationError("insufficient available margin")
            before = snapshot_contract_account(account)
            account.used_margin = Decimal(account.used_margin) + delta
            row.reserved_margin = desired
            await self.refresh_account(session, account)
            await add_contract_ledger_entry(session, account, change_type="margin_settle", amount=ZERO,
                before=before, market_id=market.id, related_order_id=row.order_id,
                note="contract_amend_margin_reserve_adjust", created_at=now)
            return
        if leverage <= ZERO:
            return
        old_reserve = old_price * old_remaining / leverage
        new_reserve = new_price * new_remaining / leverage
        delta = new_reserve - old_reserve
        if delta == 0:
            return
        margin_asset = market.margin_asset or market.quote_asset
        if fast:
            if delta > ZERO:
                try:
                    self.runtime.clearinghouse.reserve_contract_margin(user_id, margin_asset, delta)
                except ValueError as exc:
                    raise ContractValidationError(str(exc)) from exc
            else:
                self.runtime.clearinghouse.release_contract_margin(user_id, margin_asset, -delta)
            return
        account = await self.get_account(session, user_id, margin_asset)
        if delta > ZERO:
            await self._ensure_robot_quote_replay_margin(
                session,
                user_id,
                market,
                client_order_id,
                delta,
                now,
                related_order_id=related_order_id,
            )
        before = snapshot_contract_account(account)
        account.used_margin = max(ZERO, Decimal(account.used_margin) + delta)
        if Decimal(account.available_margin) < -CONTRACT_EPSILON:
            raise ContractValidationError("insufficient available margin")
        account.updated_at = now
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="margin_reserve" if delta > ZERO else "margin_release",
            amount=ZERO,
            before=before,
            market_id=market.id,
            related_order_id=related_order_id,
            note="contract_amend_margin_reserve_adjust",
            created_at=now,
        )

    async def _ensure_robot_quote_replay_margin(
        self,
        session: AsyncSession,
        user_id: int,
        market: Market,
        client_order_id: str | None,
        amount: Decimal,
        now: datetime,
        *,
        related_order_id: str | None = None,
        user_role: str | None = None,
    ) -> None:
        """Top up a Paper robot maker's durable margin before a replay reserve.

        与现货侧 ``_ensure_robot_quote_replay_capacity`` 同语义：策略机器人
        报价重放需要更多可用保证金时按缺口补足（系统账户无限资金语义），
        避免 write-behind 漂移把 writer 拖入 HALT；用户订单与 FLOW 成交订单
        不享受该补足。
        """
        if user_role is None:
            role = await session.scalar(select(User.role).where(User.id == user_id))
            user_role = str(role or "") if role is not None else None
        if not is_paper_robot_topup_eligible(user_role=user_role, client_order_id=client_order_id):
            return
        margin_asset = market.margin_asset or market.quote_asset
        account = await self.get_account(session, user_id, margin_asset)
        available = Decimal(account.available_margin or ZERO)
        shortfall = amount - available
        if shortfall <= ZERO:
            return
        before = snapshot_contract_account(account)
        account.wallet_balance = Decimal(account.wallet_balance or ZERO) + shortfall
        account.available_margin = available + shortfall
        account.updated_at = now
        await self.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="paper_maker_reserve_align",
            amount=shortfall,
            before=before,
            market_id=market.id,
            related_order_id=related_order_id,
            note="paper robot quote replay margin",
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
        """Align an ephemeral robot quote and reserve before durable fill replay."""
        order_id = str(anchor.get("order_id") or "")
        order = await session.scalar(
            select(Order).where(Order.order_id == order_id).with_for_update()
        )
        if order is None:
            raise ContractValidationError(f"maker anchor order not persisted: {order_id}")
        immutable_pairs = {
            "user_id": (int(order.user_id), int(anchor.get("user_id") or 0)),
            "market_id": (int(order.market_id), int(anchor.get("market_id") or 0)),
            "product_type": (str(order.product_type), str(anchor.get("product_type") or "")),
            "side": (str(order.side), str(anchor.get("side") or "")),
            "client_order_id": (str(order.client_order_id or ""), str(anchor.get("client_order_id") or "")),
            "position_action": (str(order.position_action or ""), str(anchor.get("position_action") or "")),
            "reduce_only": (bool(order.reduce_only), bool(anchor.get("reduce_only"))),
            "type": (str(order.type), str(anchor.get("type") or "")),
            "tif": (str(order.tif), str(anchor.get("tif") or "")),
        }
        mismatched = [name for name, values in immutable_pairs.items() if values[0] != values[1]]
        if mismatched or int(order.market_id) != int(market.id):
            raise ContractValidationError(
                f"maker anchor identity mismatch order_id={order_id} fields={mismatched}"
            )
        if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
            raise ContractValidationError(f"maker anchor is not live limit GTC: {order_id}")

        price = self._normalize_price(market, anchor.get("price"))
        quantity = self._normalize_qty(market, anchor.get("quantity"))
        filled = self._normalize_qty(market, anchor.get("filled_quantity"))
        remaining = self._normalize_qty(market, anchor.get("remaining_quantity"))
        if price is None or price <= ZERO or remaining <= ZERO:
            raise ContractValidationError(f"invalid maker pre-fill anchor quantities: {order_id}")
        # 与现货侧一致：只容忍 qty_step 级量化尘埃，真实数量漂移仍 fail-closed；
        # 归一化后按 filled+remaining 重写总量以保持耐用行恒等式。
        dust_tolerance = max(Decimal(str(market.qty_step)) * Decimal("2"), Decimal("1e-12"))
        if abs(quantity - (filled + remaining)) > dust_tolerance:
            raise ContractValidationError(f"invalid maker pre-fill anchor quantities: {order_id}")
        quantity = filled + remaining
        # 机器人报价是重启重建的临时态，快速镜像才是权威状态：耐用行可能
        # 因撤单重放、写后丢弃、批量重试等落后或超前（例如 durable 已被
        # cancel 而镜像仍持有该档）。把耐用行治愈为 anchor 状态而不是
        # fail-closed HALT；身份字段不一致仍严格拒绝。
        anchor_notional = quantize_scale(anchor.get("notional") or ZERO, 8)
        anchor_avg = to_decimal(anchor["avg_price"]) if anchor.get("avg_price") is not None else None
        anchor_sequence = int(anchor.get("sequence_number") or 0)
        anchor_version = int(anchor.get("version") or 0)
        durable_sequence = int(order.sequence_number or 0)
        durable_version = int(order.version or 0)
        # version is the order lifecycle clock, while sequence_number is the
        # price-time priority. Older fast paths could persist a stale QuoteSet
        # command sequence in the anchor. When a proven later lifecycle anchor
        # carries a lower priority, recover the remaining order at the back of
        # the market queue. Losing FIFO priority is safe; gaining an unproven
        # earlier priority is not.
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
        anchor_leverage = to_decimal(anchor.get("leverage") or order.leverage or market.default_leverage)
        durable_leverage = to_decimal(order.leverage or market.default_leverage)
        if anchor_leverage != durable_leverage:
            raise ContractValidationError(f"maker anchor leverage mismatch: {order_id}")

        old_price = self._normalize_price(market, order.price)
        if old_price is None:
            raise ContractValidationError(f"durable maker limit price missing: {order_id}")
        old_remaining = self._normalize_qty(market, order.remaining_quantity)
        if order.position_action == POSITION_ACTION_OPEN:
            margin_asset = market.margin_asset or market.quote_asset
            account = await self.get_account(session, order.user_id, margin_asset)
            reserve_delta = (price * remaining - old_price * old_remaining) / durable_leverage
            if Decimal(account.used_margin) + reserve_delta < -CONTRACT_EPSILON:
                raise ContractValidationError(f"maker anchor reserve underflow: {order_id}")
            await self._adjust_contract_reserve(
                session,
                market,
                user_id=order.user_id,
                old_price=old_price,
                old_remaining=old_remaining,
                new_price=price,
                new_remaining=remaining,
                leverage=durable_leverage,
                related_order_id=order.order_id,
                now=now,
                client_order_id=order.client_order_id,
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

    async def validate_amend(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order: Order,
        payload,
        *,
        fast: bool = False,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """Contract-specific amend validation.

        Returns (new_price, new_quantity, new_remaining, reserve_amount_delta).
        For close orders reserve delta is always zero; for open orders the delta
        is checked against the currently available margin (old reserve already
        included), which mirrors spot fast-path reserve adjustment.
        """
        if not market.is_active:
            raise ContractValidationError("market is inactive")
        if market.is_listed:
            if market.paper_status not in {"TRADING", "REDUCE_ONLY", "DELISTING"}:
                raise ContractValidationError("paper market does not allow amendments")
            if market.paper_status in {"REDUCE_ONLY", "DELISTING"} and order.position_action != POSITION_ACTION_CLOSE:
                raise ContractValidationError("paper market is reduce-only")
        trading_mode = market.contract_trading_mode or "normal"
        if trading_mode == CONTRACT_TRADING_MODE_PAUSED:
            raise ContractValidationError("contract market is paused")
        if trading_mode == CONTRACT_TRADING_MODE_REDUCE_ONLY and order.position_action == POSITION_ACTION_OPEN:
            raise ContractValidationError("contract market is reduce-only")

        new_price = self._normalize_price(market, payload.price if payload.price is not None else order.price)
        new_quantity = self._normalize_qty(market, payload.quantity)
        if new_price is None or new_price <= ZERO:
            raise ContractValidationError("price must be positive")
        if not is_step_aligned(new_price, quantize_scale(market.price_tick, market.price_precision)):
            raise ContractValidationError("price does not match tick")
        if not is_step_aligned(new_quantity, quantize_scale(market.qty_step, market.qty_precision)):
            raise ContractValidationError("quantity does not match qty_step")
        if new_quantity < Decimal(market.min_qty):
            raise ContractValidationError("quantity below min_qty")
        order_notional = new_price * new_quantity
        if order_notional < Decimal(market.min_notional) and not await min_notional_exempt(session, user, market, order):
            raise ContractValidationError("notional below min_notional")

        filled_quantity = self._normalize_qty(market, order.filled_quantity)
        new_remaining = self._normalize_qty(market, new_quantity - filled_quantity)
        if new_remaining <= ZERO:
            raise ContractValidationError("quantity cannot be below filled quantity")

        setting = await self.get_setting(session, user.id, market, persist=not fast)
        leverage = Decimal(order.leverage or setting.leverage)
        if leverage > Decimal(market.max_leverage):
            raise ContractValidationError(f"leverage exceeds max_leverage {market.max_leverage}")

        if order.position_action == POSITION_ACTION_CLOSE:
            old_remaining = self._normalize_qty(market, order.remaining_quantity)
            live_position = self.runtime.clearinghouse.position_snapshot(user.id, market.id) if fast else None
            if (
                fast
                and settings.persistence_mode == "memory"
                and str(user.role) == ROLE_BOT
                and new_remaining < old_remaining
                and live_position is not None
                and live_position.side == self.order_position_side(order.side, POSITION_ACTION_CLOSE)
                and live_position.quantity > ZERO
            ):
                # FLOW may shrink the live position while several resting
                # robot CLOSE orders still reserve the previous quantity.  In
                # that state every individual shrink would otherwise be
                # rejected because its peers still consume the old capacity,
                # so no first amendment could release the over-reservation.
                # A strict shrink cannot increase close exposure; permit it to
                # heal the ephemeral quote set.  Durable/manual validation
                # continues through the normal aggregate check below.
                return new_price, new_quantity, new_remaining, ZERO
            await self.validate_close_quantity(
                session,
                user.id,
                market,
                SimpleNamespace(side=order.side, position_action=POSITION_ACTION_CLOSE, quantity=new_remaining),
                target_side=self.order_position_side(order.side, POSITION_ACTION_CLOSE),
                hedge_mode=setting.position_mode == POSITION_MODE_HEDGE,
                fast=fast,
                exclude_order_id=str(getattr(order, "order_id", "") or "") or None,
            )
            return new_price, new_quantity, new_remaining, ZERO

        target_side = self.order_position_side(order.side, POSITION_ACTION_OPEN)
        hedge_mode = setting.position_mode == POSITION_MODE_HEDGE
        if fast:
            position_state = self.runtime.clearinghouse.position_snapshot(user.id, market.id)
            position = None
            if position_state is not None and position_state.quantity > ZERO:
                if hedge_mode or position_state.side == target_side:
                    position = SimpleNamespace(
                        quantity=position_state.quantity,
                        side=position_state.side,
                        mark_price=position_state.mark_price,
                    )
        else:
            position = await self.get_position(
                session,
                user.id,
                market,
                create=False,
                side=target_side if hedge_mode else None,
            )
        target_notional = await self.target_open_notional(session, market, position, order_notional, fast=fast)
        risk_tier = await self.risk_tier_for_notional(session, market, target_notional)
        tier_max_leverage = min(Decimal(market.max_leverage), Decimal(risk_tier.max_leverage))
        if leverage > tier_max_leverage:
            raise ContractValidationError(
                f"leverage exceeds risk tier {risk_tier.tier} max_leverage {decimal_to_str(tier_max_leverage)}"
            )

        old_remaining = self._normalize_qty(market, order.remaining_quantity)
        old_price = self._normalize_price(market, order.price) or ZERO
        old_reserve = self.reserved_margin_for_fill(order, old_remaining, old_price)
        new_reserve = new_price * new_remaining / leverage
        if not fast and not hedge_mode:
            new_reserve = await self._net_order_reserve(session, market, user.id,
                SimpleNamespace(side=order.side, position_action=order.position_action, reduce_only=order.reduce_only,
                    quantity=new_remaining, price=new_price), leverage, exclude=order.order_id)

        delta = new_reserve - old_reserve
        margin_asset = market.margin_asset or market.quote_asset
        if delta > ZERO:
            if fast:
                available_margin = self.runtime.clearinghouse.contract_available(user.id, margin_asset)
                if available_margin < delta:
                    raise ContractValidationError("insufficient available margin")
            else:
                account = await self.get_account(session, user.id, margin_asset)
                await self.refresh_account_with_ledger(
                    session,
                    account,
                    market=market,
                    note="contract_amend_validation_mark_to_market",
                )
                if Decimal(account.available_margin) < delta:
                    raise ContractValidationError("insufficient available margin")
        return new_price, new_quantity, new_remaining, delta

    async def amend_order(
        self,
        session: AsyncSession,
        user: User,
        order_id: str,
        payload,
        *,
        admin_override: bool = False,
    ) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        record = await session.execute(
            select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id)
        )
        first = record.first()
        if first is None:
            raise ContractValidationError("order not found")
        order, market = first
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ContractValidationError("order is not a contract order")
        if not admin_override and order.user_id != user.id:
            raise ContractValidationError("cannot amend others order")
        if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
            raise ContractValidationError("only gtc limit orders are amendable")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            raise ContractValidationError("order is not amendable")

        now = datetime.now(tz=UTC)
        async with self.runtime.market_financial_guard(
            market.symbol, lambda: self.contract_financial_keys(session, market, order.user_id)
        ):
            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
            await session.refresh(market)
            self._expire_financial_reads(session)
            locked = await session.execute(
                select(Order, Market)
                .join(Market, Market.id == Order.market_id)
                .where(Order.order_id == order_id)
                .with_for_update().execution_options(populate_existing=True)
            )
            locked_first = locked.first()
            if locked_first is None:
                raise ContractValidationError("order not found")
            order, market = locked_first
            if not admin_override and order.user_id != user.id:
                raise ContractValidationError("cannot amend others order")
            if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
                raise ContractValidationError("only gtc limit orders are amendable")
            if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                raise ContractValidationError("order is not amendable")
            new_price, new_quantity, new_remaining, _delta = await self.validate_amend(
                session, user, market, order, payload
            )
            current_price = self._normalize_price(market, order.price)
            current_quantity = self._normalize_qty(market, order.quantity)
            current_remaining = self._normalize_qty(market, order.remaining_quantity)
            filled_quantity = self._normalize_qty(market, order.filled_quantity)
            if current_price is None:
                raise ContractValidationError("limit order price missing")
            if new_price == current_price and new_quantity == current_quantity:
                return {"order": await self.serialize_order(session, order, market.symbol), "kept_priority": True}

            await acquire_sqlite_write_admission(session, existing_order_id=order_id)
            leverage = Decimal(order.leverage or market.default_leverage)
            await self._adjust_contract_reserve(
                session,
                market,
                user_id=order.user_id,
                old_price=current_price,
                old_remaining=current_remaining,
                new_price=new_price,
                new_remaining=new_remaining,
                leverage=leverage,
                related_order_id=order.order_id,
                now=now,
                client_order_id=order.client_order_id,
            )
            book, _, _ = await self.runtime.orderbook_snapshot(market.symbol, 1)
            best_ask = Decimal(book["asks"][0][0]) if book["asks"] else None
            best_bid = Decimal(book["bids"][0][0]) if book["bids"] else None
            crossing_book = (
                (order.side == SIDE_BUY and best_ask is not None and new_price >= best_ask)
                or (order.side == SIDE_SELL and best_bid is not None and new_price <= best_bid)
            )
            updated_orders: dict[str, Order] = {}
            impacted_users: set[int] = set()
            trade_payloads: list[dict] = []
            changed_bids: list[list[str]] = []
            changed_asks: list[list[str]] = []
            kept_priority = False
            if crossing_book:
                projected_order = SimpleNamespace(**{name: getattr(order, name) for name in (
                    "order_id", "user_id", "market_id", "product_type", "status", "position_action",
                    "reduce_only", "side", "leverage", "type", "reserved_margin")},
                    remaining_quantity=new_remaining, price=new_price)
                await self._preflight_user_makers(session, market, side=order.side,
                    quantity=new_remaining, price=new_price, stp_context=self._stp_context(user), taker_order=projected_order)
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
                if sequencer_result.cancelled_side is None or sequencer_result.engine_result is None:
                    raise ContractValidationError("order not found on book")
                result = sequencer_result.engine_result
                order.price = new_price
                order.quantity = new_quantity
                order.remaining_quantity = new_remaining
                order.sequence_number = sequencer_result.sequence_number
                order.updated_at = now
                updated_orders, impacted_users, trade_payloads, _ = await self.apply_engine_fills(
                    session=session,
                    market=market,
                    taker_user=user,
                    taker_order=order,
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
                updated_orders[order.order_id] = order
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
                await self.commit_or_rebuild_engine(session, market, reason="contract_crossing_amend_commit_failed")
            else:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    AmendOrderCommand(order_id=order.order_id, new_price=new_price, new_remaining=new_remaining)
                )
                amend = sequencer_result.amend_result
                if amend is None:
                    raise ContractValidationError("order not found on book")
                order.price = new_price
                order.quantity = new_quantity
                order.remaining_quantity = new_remaining
                order.sequence_number = int(amend.sequence_number or order.sequence_number or 0)
                order.updated_at = now
                order.status = ORDER_STATUS_PARTIALLY_FILLED if filled_quantity > ZERO else ORDER_STATUS_NEW
                order.version = int(order.version or 0) + 1
                updated_orders = {order.order_id: order}
                impacted_users = {order.user_id}
                changed_bids = amend.changed_bids
                changed_asks = amend.changed_asks
                kept_priority = bool(amend.kept_priority)
                for changed in await self.rebalance_user_orders(session, market, order.user_id, now=now):
                    updated_orders[changed.order_id] = changed
                await self.commit_or_rebuild_engine(session, market, reason="contract_amend_commit_failed")

        self._fast_metrics["slow_amend"] += 1
        self._fast_metrics["amend_success"] += 1
        if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            snap = self._fast_order_snapshot(order, market)
            self._fast_contract_orders[order.order_id] = snap
            self._fast_register_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        else:
            self._fast_contract_orders.pop(order.order_id, None)
            self._fast_unregister_client_id(order.user_id, market.id, order.client_order_id, order.order_id)
        await self.broadcast_order_flow(
            session,
            market.symbol,
            list(updated_orders.values()),
            impacted_users,
            changed_bids,
            changed_asks,
            trade_payloads,
        )
        return {
            "order": await self.serialize_order(session, order, market.symbol),
            "kept_priority": kept_priority,
            "trades": trade_payloads,
        }

    async def amend_order_batch(self, session: AsyncSession, user: User, payload) -> dict:
        from app.services.sqlite_write_admission import acquire_sqlite_write_admission

        order_ids = [item.order_id for item in payload.orders]
        if len(set(order_ids)) != len(order_ids):
            raise ContractValidationError("duplicate order_id in batch")
        symbol = str(payload.symbol).upper()
        now = datetime.now(tz=UTC)
        batch_market = await self.get_market(session, symbol)
        async with self.runtime.market_financial_guard(
            symbol, lambda: self.contract_financial_keys(session, batch_market, user.id)
        ):
            if not order_ids:
                raise ContractValidationError("orders is required")
            await acquire_sqlite_write_admission(session, existing_order_id=order_ids[0])
            await session.refresh(batch_market)
            self._expire_financial_reads(session)
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
                raise ContractValidationError(f"orders not found: {', '.join(missing)}")
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
                    raise ContractValidationError("all orders must belong to payload symbol")
                if item_market.product_type != PRODUCT_TYPE_PERP:
                    raise ContractValidationError("order is not a contract order")
                if order.user_id != user.id:
                    raise ContractValidationError("cannot amend others order")
                if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
                    raise ContractValidationError("only gtc limit orders are amendable")
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
                    raise ContractValidationError("order is not amendable")
                try:
                    new_price, new_quantity, new_remaining, _delta = await self.validate_amend(
                        session, user, item_market, order, item
                    )
                except ContractValidationError as exc:
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
                current_price = self._normalize_price(item_market, order.price)
                current_quantity = self._normalize_qty(item_market, order.quantity)
                current_remaining = self._normalize_qty(item_market, order.remaining_quantity)
                filled_quantity = self._normalize_qty(item_market, order.filled_quantity)
                book, _, _ = await self.runtime.orderbook_snapshot(item_market.symbol, 1)
                best_ask = Decimal(book["asks"][0][0]) if book["asks"] else None
                best_bid = Decimal(book["bids"][0][0]) if book["bids"] else None
                crossing_book = (
                    (order.side == SIDE_BUY and best_ask is not None and new_price >= best_ask)
                    or (order.side == SIDE_SELL and best_bid is not None and new_price <= best_bid)
                )
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
                    raise ContractValidationError("batch amend does not support crossing book")
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
                raise ContractValidationError("orders is required")
            changed_bids: list[list[str]] = []
            changed_asks: list[list[str]] = []
            if batch_items:
                await acquire_sqlite_write_admission(session, existing_order_id=batch_items[0].order_id)
                for entry in prepared:
                    if not entry["changed"]:
                        continue
                    await self._adjust_contract_reserve(
                        session,
                        entry["market"],
                        user_id=entry["order"].user_id,
                        old_price=entry["current_price"],
                        old_remaining=entry["current_remaining"],
                        new_price=entry["new_price"],
                        new_remaining=entry["new_remaining"],
                        leverage=Decimal(entry["order"].leverage or market.default_leverage),
                        related_order_id=entry["order"].order_id,
                        now=now,
                        client_order_id=entry["order"].client_order_id,
                    )
                sequencer_result = await self.runtime.get_symbol_sequencer(symbol).submit(BatchAmendCommand(batch_items))
                batch = sequencer_result.batch_amend_result
                if batch is None:
                    raise ContractValidationError("sequencer did not return batch amend result")
                failed_order_ids = [order_id for order_id, result in batch.results.items() if result is None]
                if failed_order_ids:
                    raise ContractValidationError(f"orders not found on book: {', '.join(failed_order_ids)}")
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
                await self.rebalance_user_orders(session, market, user.id, now=now)
                await self.commit_or_rebuild_engine(session, market, reason="contract_batch_amend_commit_failed")
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks

        self._fast_metrics["slow_amend"] += 1
        self._fast_metrics["amend_success"] += 1
        for entry in prepared:
            order = entry["order"]
            if order.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                snap = self._fast_order_snapshot(order, market)
                self._fast_contract_orders[order.order_id] = snap
            else:
                self._fast_contract_orders.pop(order.order_id, None)
        if prepared:
            await self.broadcast_order_flow(
                session,
                symbol,
                [entry["order"] for entry in prepared],
                {user.id},
                changed_bids,
                changed_asks,
                [],
            )
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

    async def place_order_batch(self, session: AsyncSession, user: User, payloads: list) -> dict:
        if not payloads:
            raise ContractValidationError("orders is required")
        symbols = {str(payload.symbol).upper() for payload in payloads}
        if len(symbols) != 1:
            raise ContractValidationError("all orders must belong to the same market")
        symbol = symbols.pop()
        items: list[dict] = []
        failed: list[dict] = []
        broadcast_flows: dict[str, dict] = {}
        seen_reconcile_symbols: set[str] = set()
        for index, order_payload in enumerate(payloads):
            try:
                result = await self.place_order(
                    session,
                    user,
                    order_payload,
                    broadcast=False,
                    reconcile_book=symbol not in seen_reconcile_symbols,
                )
                seen_reconcile_symbols.add(symbol)
                flow = result.pop("_broadcast_flow", None)
                if isinstance(flow, dict):
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
            except ContractValidationError as exc:
                await session.rollback()
                failed.append(
                    {
                        "index": index,
                        "client_order_id": order_payload.client_order_id,
                        "symbol": order_payload.symbol,
                        "error": str(exc),
                    }
                )
        for flow_symbol, flow in broadcast_flows.items():
            await self.broadcast_order_flow(
                session,
                flow_symbol,
                flow["orders"],
                flow["impacted_users"],
                flow["changed_bids"],
                flow["changed_asks"],
                flow["trade_payloads"],
            )
        return {
            "ok": not failed,
            "requested_count": len(payloads),
            "accepted_count": len(items),
            "failed_count": len(failed),
            "items": items,
            "failed": failed,
        }

    async def place_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        payload,
        plan: dict,
    ) -> dict:
        """Write-behind replay of a fast-path contract order (no engine mutation)."""
        now = datetime.fromisoformat(plan["now"])
        order = await self.create_order_record(
            session,
            user=user,
            market=market,
            payload=payload,
            order_id=plan["order_id"],
            status=ORDER_STATUS_NEW,
            leverage=to_decimal(plan.get("leverage")) if plan.get("leverage") is not None else None,
            reject_reason=None,
            created_at=now,
            sequence_number=int(plan["sequence_number"]),
        )
        result = engine_result_from_plan(plan["engine_result"])
        reserve = ContractReservePlan(
            amount=Decimal(str(plan["reserve"]["amount"])),
            leverage=to_decimal(plan["reserve"].get("leverage")) if plan["reserve"].get("leverage") is not None else market.default_leverage,
        )
        order.reserved_margin = reserve.amount
        account = await self.get_account(session, user.id, market.margin_asset or market.quote_asset)
        if reserve.amount > ZERO:
            await self._ensure_robot_quote_replay_margin(
                session,
                user.id,
                market,
                payload.client_order_id,
                reserve.amount,
                now,
                related_order_id=order.order_id,
            )
            before = snapshot_contract_account(account)
            account.used_margin = Decimal(account.used_margin) + reserve.amount
            await self.refresh_account(session, account)
            if Decimal(account.available_margin) < -CONTRACT_EPSILON:
                raise ContractValidationError("insufficient available margin")
            await add_contract_ledger_entry(
                session,
                account,
                change_type="margin_reserve",
                amount=ZERO,
                before=before,
                market_id=market.id,
                related_order_id=order.order_id,
                note="contract_order_initial_margin_reserve",
                created_at=now,
            )
        try:
            updated_orders, _impacted_users, _trade_payloads, total_notional = await self.apply_engine_fills(
                session=session,
                market=market,
                taker_user=user,
                taker_order=order,
                result=result,
                executed_at=now,
                ingest=False,
            )
        except Exception:
            await session.rollback()
            raise
        order.notional = total_notional
        order.filled_quantity = self._normalize_qty(market, order.filled_quantity)
        order.avg_price = (total_notional / Decimal(order.filled_quantity)) if Decimal(order.filled_quantity) > ZERO else None
        order.remaining_quantity = self._normalize_qty(market, result.remaining_quantity)
        order.updated_at = now
        if Decimal(order.filled_quantity) == ZERO:
            order.status = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
        elif Decimal(order.remaining_quantity) == ZERO:
            order.status = ORDER_STATUS_FILLED
        elif result.placed_on_book:
            order.status = ORDER_STATUS_PARTIALLY_FILLED
        else:
            order.status = ORDER_STATUS_CANCELED
            order.canceled_at = now
        await self.release_leftover(session, market, order, reserve, now)
        await self.refresh_account_with_ledger(
            session,
            account,
            market=market,
            note="contract_place_final_mark_to_market",
            created_at=now,
        )
        await session.flush()
        return {"order_id": order.order_id, "status": order.status}

    async def cancel_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order_id: str,
        plan: dict,
    ) -> dict:
        locked = await session.execute(
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(Order.order_id == order_id)
            .with_for_update()
        )
        first = locked.first()
        if first is None:
            raise ContractValidationError("order not found")
        order, order_market = first
        if order_market.symbol != market.symbol:
            raise ContractValidationError("order market mismatch")
        if order.user_id != user.id:
            raise ContractValidationError("cannot cancel others order")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            # Write-behind replay may arrive after the order reached a terminal
            # state (cancel/replace raced the writer); treat as already applied.
            return {"order_id": order.order_id, "status": order.status}
        now = datetime.fromisoformat(plan["now"])
        if order.position_action == POSITION_ACTION_OPEN:
            release = self.reserved_margin_for_fill(
                order,
                Decimal(order.remaining_quantity),
                Decimal(order.price or order.reference_price or 0),
            )
            account = await self.get_account(session, order.user_id, market.margin_asset or market.quote_asset)
            before = snapshot_contract_account(account)
            account.used_margin = Decimal(account.used_margin) - min(max(release, ZERO), Decimal(account.used_margin))
            await self.refresh_account(session, account)
            await add_contract_ledger_entry(
                session,
                account,
                change_type="margin_release",
                amount=ZERO,
                before=before,
                market_id=market.id,
                related_order_id=order.order_id,
                note="contract_order_cancel_margin_release",
                created_at=now,
            )
        order.status = ORDER_STATUS_CANCELED
        order.canceled_at = now
        order.updated_at = now
        order.version = int(order.version or 0) + 1
        await session.flush()
        return {"order_id": order.order_id, "status": order.status}

    async def amend_order_replay(
        self,
        session: AsyncSession,
        user: User,
        market: Market,
        order_id: str,
        payload,
        plan: dict,
    ) -> dict:
        locked = await session.execute(
            select(Order, Market)
            .join(Market, Market.id == Order.market_id)
            .where(Order.order_id == order_id)
            .with_for_update()
        )
        first = locked.first()
        if first is None:
            raise ContractValidationError("order not found")
        order, order_market = first
        if order_market.symbol != market.symbol:
            raise ContractValidationError("order market mismatch")
        if order.user_id != user.id:
            raise ContractValidationError("cannot amend others order")
        if order.type != ORDER_TYPE_LIMIT or order.tif not in {TIF_GTC, TIF_POST_ONLY}:
            raise ContractValidationError("only gtc limit orders are amendable")
        if order.status not in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
            return {"order_id": order.order_id, "status": order.status}
        now = datetime.fromisoformat(plan["now"])
        new_price = self._normalize_price(order_market, payload.price if payload.price is not None else order.price)
        new_quantity = self._normalize_qty(order_market, payload.quantity)
        new_remaining = self._normalize_qty(order_market, plan["new_remaining"])
        old_price = self._normalize_price(order_market, order.price)
        old_remaining = self._normalize_qty(order_market, order.remaining_quantity)
        if old_price is None or new_price is None:
            raise ContractValidationError("limit order price missing")
        await self._adjust_contract_reserve(
            session,
            order_market,
            user_id=order.user_id,
            old_price=old_price,
            old_remaining=old_remaining,
            new_price=new_price,
            new_remaining=new_remaining,
            leverage=Decimal(order.leverage or order_market.default_leverage),
            related_order_id=order.order_id,
            now=now,
            client_order_id=order.client_order_id,
        )
        order.price = new_price
        order.quantity = new_quantity
        order.remaining_quantity = new_remaining
        order.sequence_number = int(plan["sequence_number"])
        order.updated_at = now
        order.version = int(order.version or 0) + 1
        if plan.get("amend_kind") == "cross":
            result = engine_result_from_plan(plan["engine_result"])
            await self.apply_engine_fills(
                session=session,
                market=order_market,
                taker_user=user,
                taker_order=order,
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

    # ------------------------------------------------------------------
    # Contract fast path (memory clearinghouse + write-behind persistence)
    # ------------------------------------------------------------------
    @staticmethod
    def _fast_engine_plan(result: EngineResult) -> dict:
        return {
            "remaining_quantity": decimal_to_str(result.remaining_quantity),
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

    @staticmethod
    def _fast_reserved_margin_for_snap(snap: dict, quantity: Decimal, price: Decimal, leverage: Decimal) -> Decimal:
        if snap.get("position_action") != POSITION_ACTION_OPEN:
            return ZERO
        reserve_price = Decimal(snap["price"]) if snap.get("type") == ORDER_TYPE_LIMIT and snap.get("price") else price
        return reserve_price * quantity / leverage

    async def _fast_settle_side(
        self,
        session: AsyncSession,
        market: Market,
        snap: dict,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        executed_at: datetime,
    ) -> Decimal:
        user_id = int(snap["user_id"])
        margin_asset = market.margin_asset or market.quote_asset
        account = self.runtime.clearinghouse.ensure_contract_account(user_id, margin_asset)
        leverage = Decimal(snap.get("leverage") or market.default_leverage)
        position = self.runtime.clearinghouse.position_snapshot(user_id, int(market.id))
        old_unrealized = position.unrealized() if position is not None else ZERO
        if position is None:
            position = PositionState(user_id=user_id, market_id=int(market.id), side=POSITION_SIDE_FLAT,
                quantity=ZERO, entry_price=ZERO, isolated_margin=ZERO, maintenance_margin=ZERO, leverage=leverage)
        try:
            split = split_fill(side=snap["side"], action=snap.get("position_action"),
                reduce_only=bool(snap.get("reduce_only")),
                hedge=await self.is_hedge_position_mode(session, user_id, market),
                position_side=position.side,
                position_qty=self._position_qty_without_storage_dust(market, position.quantity), quantity=quantity)
        except ValueError as exc:
            raise ContractValidationError(str(exc)) from exc
        snap["_last_fill_action"] = split.action
        realized = ZERO
        released_margin = ZERO
        if split.close:
            old_qty = self._position_qty_without_storage_dust(market, position.quantity)
            realized = (price - Decimal(position.entry_price)) * split.close * (1 if position.side == POSITION_SIDE_LONG else -1)
            released_margin = Decimal(position.isolated_margin) * split.close / old_qty
            position.quantity = old_qty - split.close
            position.isolated_margin = Decimal(position.isolated_margin) - released_margin
            position.realized_pnl = Decimal(position.realized_pnl) + realized
            if position.quantity <= CONTRACT_EPSILON:
                position.side, position.quantity, position.entry_price, position.isolated_margin = POSITION_SIDE_FLAT, ZERO, ZERO, ZERO
        added_margin = price * split.opened / leverage
        if split.opened:
            old_qty = Decimal(position.quantity)
            position.entry_price = (Decimal(position.entry_price)*old_qty + price*split.opened)/(old_qty+split.opened)
            position.quantity = old_qty + split.opened
            position.side = self.order_position_side(snap["side"], POSITION_ACTION_OPEN)
            position.isolated_margin = Decimal(position.isolated_margin) + added_margin
            position.leverage = leverage
        reserved = self._fast_reserved_margin_for_snap(snap, quantity, price, leverage)
        account.wallet_balance = Decimal(account.wallet_balance) + realized
        account.used_margin = Decimal(account.used_margin) + added_margin - released_margin - reserved
        account.realized_pnl = Decimal(account.realized_pnl) + realized

        account.wallet_balance = Decimal(account.wallet_balance) - fee
        account.total_fees = Decimal(account.total_fees) + fee
        mark = self.mark_price(market)
        position.mark_price = mark
        if Decimal(position.quantity) <= ZERO or position.side == POSITION_SIDE_FLAT:
            position.maintenance_margin = ZERO
            new_unrealized = ZERO
        else:
            notional = mark * Decimal(position.quantity)
            risk_tier = await self.risk_tier_for_notional(session, market, notional)
            position.maintenance_margin = self.maintenance_margin_for_notional(notional, risk_tier)
            new_unrealized = position.unrealized(mark)
        account.unrealized_pnl = Decimal(account.unrealized_pnl) + (new_unrealized - old_unrealized)
        self.runtime.clearinghouse.upsert_position(position)
        return realized

    @staticmethod
    def _fast_trade_source(taker_snap: dict, maker_snap: dict) -> str:
        ids = [str(taker_snap.get("client_order_id") or ""), str(maker_snap.get("client_order_id") or "")]
        if any(value.startswith(("flowv2-", "flow-", "flowioc-", "perpflow-")) or "-flow-" in value for value in ids):
            return "flow"
        if any(value.startswith(BOT_ORDER_PREFIXES) for value in ids):
            return "bot"
        return "user"

    async def _fast_apply_contract_fill_settlement(
        self,
        *,
        session: AsyncSession,
        market: Market,
        taker_snap: dict,
        result: EngineResult,
        executed_at: datetime,
    ) -> tuple[list[dict], set[int], Decimal, list[dict]]:
        trade_payloads: list[dict] = []
        impacted: set[int] = {int(taker_snap["user_id"])}
        total_notional = ZERO
        maker_pre_fill_anchors: dict[str, dict] = {}
        taker_fee_rate = await self.get_fee_rate(session, int(taker_snap["user_id"]), market.id, taker=True, market=market)
        maker_fee_rates: dict[int, Decimal] = {}
        margin_asset = market.margin_asset or market.quote_asset
        for fill in result.fills:
            maker = self._fast_contract_orders.get(fill.maker_order_id)
            if maker is None:
                # Resting maker may belong to a legacy/slow-path user or the
                # writer may not have replayed it yet; load from DB so the
                # write-behind task can still settle deterministically.
                row = await session.execute(
                    select(Order, Market)
                    .join(Market, Market.id == Order.market_id)
                    .where(Order.order_id == fill.maker_order_id)
                )
                db_record = row.first()
                if db_record is None:
                    raise ContractValidationError(f"fast maker order not found: {fill.maker_order_id}")
                db_order, db_market = db_record
                maker = self._fast_order_snapshot(db_order, db_market)
                self._fast_contract_orders[fill.maker_order_id] = maker
            maker_user_id = int(maker["user_id"])
            if str(fill.maker_order_id) not in maker_pre_fill_anchors:
                anchor = self._fast_maker_pre_fill_anchor(maker)
                anchor["user_role"] = await session.scalar(
                    select(User.role).where(User.id == maker_user_id)
                )
                maker_pre_fill_anchors[str(fill.maker_order_id)] = anchor
            impacted.add(maker_user_id)
            if maker_user_id not in maker_fee_rates:
                maker_fee_rates[maker_user_id] = await self.get_fee_rate(session, maker_user_id, market.id, taker=False, market=market)
            quote_amount = Decimal(fill.price) * Decimal(fill.quantity)
            total_notional += quote_amount
            maker_fee = quote_amount * maker_fee_rates[maker_user_id]
            taker_fee = quote_amount * taker_fee_rate
            trade_id = next_trade_id()
            maker_realized = await self._fast_settle_side(
                session, market, maker, Decimal(fill.quantity), Decimal(fill.price), maker_fee, executed_at
            )
            taker_realized = await self._fast_settle_side(
                session, market, taker_snap, Decimal(fill.quantity), Decimal(fill.price), taker_fee, executed_at
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
                    "taker_position_action": taker_snap.get("_last_fill_action", taker_snap.get("position_action")),
                    "maker_position_action": maker.get("_last_fill_action", maker.get("position_action")),
                    "taker_realized_pnl": decimal_to_str(quantize_scale(taker_realized, 8)),
                    "maker_realized_pnl": decimal_to_str(quantize_scale(maker_realized, 8)),
                    "maker_fee": decimal_to_str(quantize_scale(maker_fee, 8)),
                    "taker_fee": decimal_to_str(quantize_scale(taker_fee, 8)),
                    "maker_fee_asset": margin_asset,
                    "taker_fee_asset": margin_asset,
                    "source": self._fast_trade_source(taker_snap, maker),
                    "executed_at": to_millis(executed_at),
                }
            )
        return trade_payloads, impacted, total_notional, list(maker_pre_fill_anchors.values())

    def _fast_contract_account_payload(self, user_id: int, margin_asset: str) -> dict:
        state = self.runtime.clearinghouse.contract_snapshot(user_id, margin_asset)
        if state is None:
            return {
                "user_id": user_id,
                "margin_asset": margin_asset,
                "wallet_balance": "0",
                "available_margin": "0",
                "used_margin": "0",
                "unrealized_pnl": "0",
                "realized_pnl": "0",
                "total_fees": "0",
                "updated_at": None,
            }
        return {
            "user_id": user_id,
            "margin_asset": margin_asset,
            "wallet_balance": decimal_to_str(Decimal(state.wallet_balance)),
            "available_margin": decimal_to_str(Decimal(state.available_margin)),
            "used_margin": decimal_to_str(Decimal(state.used_margin)),
            "unrealized_pnl": decimal_to_str(Decimal(state.unrealized_pnl)),
            "realized_pnl": decimal_to_str(Decimal(state.realized_pnl)),
            "total_fees": decimal_to_str(Decimal(state.total_fees)),
            "updated_at": to_millis(datetime.now(tz=UTC)),
        }

    async def _fast_broadcast_contract(
        self,
        market: Market,
        *,
        changed_bids: list[list[str]],
        changed_asks: list[list[str]],
        trade_payloads: list[dict],
        order_snaps: list[dict],
        impacted_users: set[int],
    ) -> None:
        old_published = self.runtime.published_orderbooks.get(market.symbol)
        snapshot, seq, updated_at_ms = self.runtime.orderbook_snapshot_unlocked(market.symbol, 100)
        if not (changed_bids or changed_asks or trade_payloads or order_snaps):
            return
        # PublishedBook is already updated synchronously. Defer expensive
        # delta JSON/private WS fan-out so a 50ms QuotePatch is not coupled to
        # a slow consumer; WebSocketManager applies latest-wins and timeout
        # protection at the actual send boundary.
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
                "order_snaps": order_snaps,
                "impacted_users": impacted_users,
            },
            merge=self._merge_fast_broadcast_payload,
        )
        # Yield once so in-process adapters observe the scheduled publication;
        # actual socket delivery remains deferred and latest-wins.
        await asyncio.sleep(0)

    async def _deferred_fast_broadcast_contract(
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
        order_snaps: list[dict],
        impacted_users: set[int],
    ) -> None:
        if old_published is None:
            orderbook_payload = self.runtime.orderbook_snapshot_payload(market.symbol, source="fast_snapshot")
            content_changed = bool(snapshot["bids"] or snapshot["asks"])
        else:
            orderbook_payload = self.runtime.orderbook_delta_payload(
                market.symbol,
                old_published,
                source="fast_delta",
            )
            content_changed = bool(orderbook_payload.get("bids") or orderbook_payload.get("asks"))
        if content_changed and (changed_bids or changed_asks):
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
            await self.runtime.market_data.broadcast_klines(self.runtime.ws, market.symbol)
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
        for user_id in impacted_users:
            for snap in order_snaps:
                if int(snap["user_id"]) == user_id:
                    await self.runtime.ws.broadcast_private(
                        user_id,
                        "orders",
                        {"channel": "orders", "type": "update", "data": self._fast_serialize_order(market, snap)},
                    )
            await self.runtime.ws.broadcast_private(
                user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "account",
                    "data": self._fast_contract_account_payload(user_id, market.margin_asset or market.quote_asset),
                    "ts": to_millis(datetime.now(tz=UTC)),
                },
            )

    def _fast_ingest_trade_payloads(
        self,
        market: Market,
        trade_payloads: list[dict],
        *,
        executed_at: datetime,
        taker_side: str,
    ) -> None:
        """Publish completed fast settlement into live recent trades/K-lines."""
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

    async def _fast_restore_market(self, session: AsyncSession, market_id: int, *, reason: str) -> None:
        """Defensive recovery after a fast-path settlement failure.

        Rebuilds the engine book and fast mirror from DB, and reloads the
        clearinghouse mirror from DB so a failed hot-path mutation cannot leave
        memory ahead of SQLite.
        """
        await self.rebuild_engine_book_from_db(session, market_id, reason=reason)
        await self.runtime.clearinghouse.refresh_contract_market(session, market_id)
        market = await session.scalar(select(Market).where(Market.id == int(market_id)))
        if market is None:
            return
        db_order_ids = await self.open_order_ids_for_market(session, market)
        for order_id in [
            order_id
            for order_id, snap in self._fast_contract_orders.items()
            if int(snap.get("market_id", -1)) == int(market_id)
        ]:
            if order_id not in db_order_ids:
                snap = self._fast_contract_orders.pop(order_id, None)
                if snap is not None:
                    self._fast_unregister_client_id(
                        int(snap["user_id"]),
                        int(market.id),
                        snap.get("client_order_id"),
                        order_id,
                    )
        rows = await session.execute(
            select(Order).where(
                Order.market_id == int(market_id),
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.type == ORDER_TYPE_LIMIT,
                Order.tif.in_([TIF_GTC, TIF_POST_ONLY]),
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
                Order.remaining_quantity > ZERO,
            )
        )
        for order in rows.scalars():
            if order.order_id in self._fast_contract_orders and await self.is_contract_ladder(session, int(order.user_id), int(market.id)):
                continue
            snap = self._fast_order_snapshot(order, market)
            self._fast_contract_orders[order.order_id] = snap
            self._fast_register_client_id(int(order.user_id), int(market.id), order.client_order_id, order.order_id)

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
        quote_validation_context: dict | None = None,
    ) -> dict | None:
        if not self._fast_writer_ready():
            logging.getLogger("contract_service").warning(
                "fast place skipped: persistence writer not ready user_id=%s symbol=%s client_order_id=%s",
                user.id,
                getattr(payload, "symbol", "?"),
                getattr(payload, "client_order_id", None),
            )
            return None
        market = market_override or await self.get_market(session, payload.symbol)
        # Internal ladder orders use the ordinary sequencer adapter and the
        # existing synchronous user finance transaction. Decline before ANY
        # matching/reserve/synthetic mutation so ordinary user API callers can
        # safely fall back to their durable slow path.
        ladder_market = any(mid == int(market.id) for _uid, mid in self._ladder_bindings)
        if not ladder_market and platform_durable_contract():
            ladder_market = await session.scalar(select(MarketBotAccount.id).where(
                MarketBotAccount.market_id == market.id,
                MarketBotAccount.strategy_role == "CONTRACT_LADDER",
            ).limit(1)) is not None
        if ladder_market:
            return None
        if platform_durable_contract():
            # All platform account-bearing executions share market + UID/asset
            # locks and one committed SQL authority. Legacy memory clearing uses
            # a disjoint global lock; mixing the two can invalidate a net reserve
            # while waiting. LADDER's ephemeral adapter was declined above.
            return await self.place_order(session, user, payload, now=now, broadcast=broadcast)
        if broadcast:
            try:
                synthetic = await execute_synthetic_flow(
                    session=session,
                    runtime=self.runtime,
                    market=market,
                    user=user,
                    payload=payload,
                    fast_orders=self._fast_contract_orders,
                )
            except SyntheticFlowUnavailableError:
                return None
            except SyntheticFlowValidationError as exc:
                raise ContractValidationError(str(exc)) from exc
            if synthetic is not None:
                is_new = bool(synthetic.pop("_synthetic_new", False))
                if is_new:
                    await broadcast_synthetic_flow(self.runtime, market, synthetic["trades"])
                return synthetic
            if not self._fast_writer_ready():
                return None
        now = now or datetime.now(tz=UTC)
        self._normalize_payload(market, payload)
        order_id = str(order_id or next_order_id())
        if payload.client_order_id and not allow_client_order_reuse:
            client_key = (int(user.id), int(market.id), str(payload.client_order_id))
            existing_order_id = self._fast_contract_client_ids.get(client_key)
            if existing_order_id is not None:
                snap = self._fast_contract_orders.get(existing_order_id)
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
                    # Stale mirror (slow-path cancel/amend raced with the writer):
                    # drop it and fall through to a fresh create/DB idempotency.
                    self._fast_contract_orders.pop(existing_order_id, None)
                    self._fast_unregister_client_id(int(user.id), int(market.id), payload.client_order_id, existing_order_id)
            existing = await self._load_live_client_order(
                session, user, market, payload.client_order_id
            )
            if existing is not None:
                if (
                    order_id
                    and str(existing.order_id) == str(order_id)
                    and self._fast_contract_orders.get(order_id) is None
                ):
                    # The live DB row was created by the write-behind replay of
                    # the current QuoteSet command before this operation reached
                    # the engine.  Treating it as a resting engine order would
                    # silently skip the engine placement and leave the level
                    # missing from the book (subsequent amends then fail with
                    # "engine amend returned None").  Fall through and place it
                    # into the engine; the replay duplicate guard skips the DB
                    # insert because the row already exists.
                    logging.getLogger("contract_service").info(
                        "fast place idempotency bypassed for write-behind replay "
                        "user_id=%s order_id=%s client_order_id=%s",
                        user.id,
                        order_id,
                        payload.client_order_id,
                    )
                else:
                    snap = self._fast_order_snapshot(existing, market)
                    self._fast_contract_orders[existing.order_id] = snap
                    self._fast_register_client_id(int(user.id), int(market.id), payload.client_order_id, existing.order_id)
                    return {
                        "order": self._fast_serialize_order(market, snap),
                        "idempotent": True,
                        "fast_path": True,
                    }
        reserve = await self.validate_order(
            session,
            user,
            market,
            payload,
            fast=True,
            quote_context=quote_validation_context if allow_client_order_reuse else None,
        )
        reference_price = self.mark_price(market)
        effective_command_sequence = command_sequence
        if (
            command_sequence is not None
            and int(command_sequence) <= int(self.runtime.sequence_numbers[market.symbol])
        ):
            if (settings.persistence_mode == "memory" and user.role == ROLE_BOT):
                effective_command_sequence = None
            else:
                raise ContractValidationError("stale place priority sequence")
        async with self.runtime.fast_matching_guard(market.symbol):
            if reserve.amount > ZERO:
                try:
                    self.runtime.clearinghouse.reserve_contract_margin(
                        user.id, market.margin_asset or market.quote_asset, reserve.amount
                    )
                except ValueError as exc:
                    raise ContractValidationError(str(exc)) from exc
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
                        maker_guard=lambda _order_id, maker_user_id: self._contract_maker_margin_ok(
                            maker_user_id, int(market.id)
                        ),
                        sequence_number=effective_command_sequence,
                        **self._stp_context(user),
                    )
                )
            except NotExecuted:
                if reserve.amount > ZERO:
                    self.runtime.clearinghouse.release_contract_margin(user.id, market.margin_asset or market.quote_asset, reserve.amount)
                raise
            result = sequencer_result.engine_result
            if result is None:
                raise ContractValidationError("sequencer did not return engine result")
            taker_snap = {
                "order_id": order_id,
                "client_order_id": payload.client_order_id,
                "user_id": int(user.id),
                "market_id": int(market.id),
                "symbol": market.symbol,
                "product_type": market.product_type,
                "side": payload.side,
                "position_action": payload.position_action,
                "reduce_only": bool(payload.reduce_only),
                "leverage": decimal_to_str(reserve.leverage),
                "type": payload.type,
                "tif": payload.tif,
                "status": ORDER_STATUS_NEW,
                "sequence_number": sequencer_result.sequence_number,
                "version": 0,
                "price": decimal_to_str(payload.price) if payload.price is not None else None,
                "quantity": decimal_to_str(payload.quantity),
                "filled_quantity": "0",
                "remaining_quantity": decimal_to_str(payload.quantity),
                "avg_price": None,
                "notional": "0",
                "reference_price": decimal_to_str(reference_price),
                "reject_reason": None,
                "created_at": to_millis(now),
                "updated_at": to_millis(now),
            }
            try:
                (
                    trade_payloads,
                    impacted,
                    total_notional,
                    maker_pre_fill_anchors,
                ) = await self._fast_apply_contract_fill_settlement(
                    session=session,
                    market=market,
                    taker_snap=taker_snap,
                    result=result,
                    executed_at=now,
                )
            except Exception as exc:
                logging.getLogger("contract_service").warning(
                    "contract fast place settlement failed, restoring from DB: %s",
                    exc,
                )
                if reserve.amount > ZERO:
                    self.runtime.clearinghouse.release_contract_margin(
                        user.id, market.margin_asset or market.quote_asset, reserve.amount
                    )
                await self._fast_restore_market(session, int(market.id), reason="contract_fast_place_settlement_failed")
                raise ContractValidationError(
                    f"contract place settlement rejected: {exc}"
                ) from exc
            taker_snap["notional"] = decimal_to_str(total_notional)
            taker_snap["remaining_quantity"] = decimal_to_str(result.remaining_quantity)
            if Decimal(taker_snap["filled_quantity"]) == ZERO:
                taker_snap["status"] = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
            elif Decimal(taker_snap["remaining_quantity"]) == ZERO:
                taker_snap["status"] = ORDER_STATUS_FILLED
            elif result.placed_on_book:
                taker_snap["status"] = ORDER_STATUS_PARTIALLY_FILLED
            else:
                taker_snap["status"] = ORDER_STATUS_CANCELED
            if taker_snap["status"] in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}:
                self._fast_contract_orders[order_id] = taker_snap
                self._fast_register_client_id(int(user.id), int(market.id), payload.client_order_id, order_id)
            else:
                self._fast_unregister_client_id(int(user.id), int(market.id), payload.client_order_id, order_id)
            task = {
                "kind": "contract_place",
                "symbol": market.symbol,
                "user_id": int(user.id),
                "order_id": order_id,
                "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload),
                "engine_result": self._fast_engine_plan(result),
                "sequence_number": sequencer_result.sequence_number,
                "now": now.isoformat(),
                "leverage": decimal_to_str(reserve.leverage),
                "reference_price": decimal_to_str(reference_price),
                "reserve": {
                    "amount": decimal_to_str(reserve.amount),
                    "leverage": decimal_to_str(reserve.leverage),
                },
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
            await self._fast_broadcast_contract(
                market,
                changed_bids=result.changed_bids,
                changed_asks=result.changed_asks,
                trade_payloads=trade_payloads,
                order_snaps=[taker_snap],
                impacted_users=impacted,
            )
        self._fast_metrics["fast_place"] += 1
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
                "order_snaps": [taker_snap],
                "impacted_users": impacted,
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
        snap = self._fast_contract_orders.get(order_id)
        if snap is None:
            logging.getLogger("contract_service").warning(
                "fast cancel skipped: order not found on fast mirror user_id=%s order_id=%s",
                user.id,
                order_id,
            )
            return None
        if platform_durable_contract() or (snap is not None and snap.get("reserved_margin") is not None):
            return await self.cancel_order(session, user, order_id)
        if not self._fast_writer_ready():
            logging.getLogger("contract_service").warning(
                "fast cancel skipped: persistence writer not ready user_id=%s order_id=%s",
                user.id,
                order_id,
            )
            return None
        market = await self.get_market(session, snap["symbol"])
        now = now or datetime.now(tz=UTC)
        async with self.runtime.fast_matching_guard(market.symbol):
            sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                CancelOrderCommand(order_id=order_id, sequence_number=command_sequence)
            )
            side_name = sequencer_result.cancelled_side
            if side_name is None:
                logging.getLogger("contract_service").warning(
                    "fast cancel skipped: engine has no such order user_id=%s order_id=%s",
                    user.id,
                    order_id,
                )
                return None
            changes = sequencer_result.changed_bids if side_name == SIDE_BUY else sequencer_result.changed_asks
            if snap.get("position_action") == POSITION_ACTION_OPEN and snap.get("price"):
                release = Decimal(snap["price"]) * Decimal(snap["remaining_quantity"]) / Decimal(
                    snap.get("leverage") or market.default_leverage
                )
                if release > ZERO:
                    self.runtime.clearinghouse.release_contract_margin(
                        int(snap["user_id"]), market.margin_asset or market.quote_asset, release
                    )
            snap["status"] = ORDER_STATUS_CANCELED
            snap["updated_at"] = to_millis(now)
            snap["version"] = int(snap["version"] or 0) + 1
            self._fast_unregister_client_id(
                int(snap["user_id"]), int(market.id), snap.get("client_order_id"), order_id
            )
            self._fast_contract_orders.pop(order_id, None)
            task = {
                "kind": "contract_cancel",
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
            await self._fast_broadcast_contract(
                market,
                changed_bids=changed_bids,
                changed_asks=changed_asks,
                trade_payloads=[],
                order_snaps=[snap],
                impacted_users={int(snap["user_id"])},
            )
        self._fast_metrics["fast_cancel"] += 1
        response = {"order": self._fast_serialize_order(market, snap), "fast_path": True}
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": [],
                "order_snaps": [snap],
                "impacted_users": {int(snap["user_id"])},
            }
        return response

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
        if not self._fast_writer_ready():
            logging.getLogger("contract_service").warning(
                "fast amend skipped: persistence writer not ready user_id=%s order_id=%s",
                user.id,
                order_id,
            )
            return None
        snap = self._fast_contract_orders.get(order_id)
        if snap is None:
            durable = await session.scalar(select(Order).where(Order.order_id == order_id))
            if (
                durable is not None
                and int(durable.user_id) == int(user.id)
                and durable.product_type == PRODUCT_TYPE_PERP
                and durable.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                and Decimal(durable.remaining_quantity) > ZERO
            ):
                logging.getLogger("contract_service").error(
                    "fast amend unavailable: durable live order missing from mirror user_id=%s order_id=%s",
                    user.id,
                    order_id,
                )
                return None
            logging.getLogger("contract_service").warning(
                "fast amend skipped: order not found on fast mirror user_id=%s order_id=%s",
                user.id,
                order_id,
            )
            raise ContractValidationError("order not found on fast book")
        if platform_durable_contract() or (snap is not None and snap.get("reserved_margin") is not None):
            return await self.amend_order(session, user, order_id, payload)
        market = market_override or await self.get_market(session, snap["symbol"])
        now = now or datetime.now(tz=UTC)
        new_price = self._normalize_price(market, payload.price if payload.price is not None else snap["price"])
        new_quantity = self._normalize_qty(market, payload.quantity)
        filled = Decimal(snap["filled_quantity"])
        new_remaining = self._normalize_qty(market, new_quantity - filled)
        old_price = Decimal(snap["price"]) if snap.get("price") else ZERO
        old_remaining = Decimal(snap["remaining_quantity"])
        if old_price <= ZERO or new_price is None or new_price <= ZERO:
            logging.getLogger("contract_service").warning(
                "fast amend skipped: invalid limit price user_id=%s order_id=%s old_price=%s new_price=%s",
                user.id,
                order_id,
                old_price,
                new_price,
            )
            return None
        pseudo_order = SimpleNamespace(
            order_id=order_id,
            user_id=int(snap["user_id"]),
            side=snap["side"],
            position_action=snap["position_action"],
            reduce_only=bool(snap.get("reduce_only")),
            type=snap["type"],
            tif=snap["tif"],
            status=snap["status"],
            sequence_number=int(snap.get("sequence_number") or 0),
            version=int(snap.get("version") or 0),
            price=decimal_to_str(old_price),
            quantity=snap["quantity"],
            filled_quantity=snap["filled_quantity"],
            remaining_quantity=snap["remaining_quantity"],
            leverage=snap.get("leverage"),
        )
        await self.validate_amend(session, user, market, pseudo_order, payload, fast=True)
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
                # QuoteSet operation sequences are planned ahead of execution;
                # a concurrent fast batch may advance the live symbol clock in
                # the meantime.  Allocate at execution time rather than
                # regressing price-time priority.
                effective_command_sequence = None
            else:
                raise ContractValidationError("stale amend priority sequence")
        async with self.runtime.fast_matching_guard(market.symbol):
            maker_pre_fill_anchors: list[dict] = []
            reserve_aligned_before_fill = False
            if crossing_book:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    CrossingAmendCommand(
                        order_id=order_id,
                        user_id=int(snap["user_id"]),
                        side=snap["side"],
                        new_price=new_price,
                        new_remaining=new_remaining,
                        created_at=now,
                        sequence_number=effective_command_sequence,
                        **self._stp_context(user),
                    )
                )
                if sequencer_result.cancelled_side is None or sequencer_result.engine_result is None:
                    logging.getLogger("contract_service").warning(
                        "fast crossing amend skipped: engine returned no result user_id=%s order_id=%s "
                        "cancelled_side=%s engine_result=%s",
                        user.id,
                        order_id,
                        sequencer_result.cancelled_side,
                        sequencer_result.engine_result,
                    )
                    return None
                result = sequencer_result.engine_result
                # The crossing amend becomes the taker at its new price/size.
                # Align the in-memory reserve and order snapshot before fill
                # settlement so reserved-margin consumption uses that state.
                leverage = Decimal(snap.get("leverage") or market.default_leverage)
                old_reserve = old_price * old_remaining / leverage
                new_reserve = new_price * new_remaining / leverage
                delta = new_reserve - old_reserve
                if delta > ZERO:
                    try:
                        self.runtime.clearinghouse.reserve_contract_margin(
                            int(snap["user_id"]), market.margin_asset or market.quote_asset, delta
                        )
                    except ValueError as exc:
                        raise ContractValidationError(str(exc)) from exc
                elif delta < ZERO:
                    self.runtime.clearinghouse.release_contract_margin(
                        int(snap["user_id"]), market.margin_asset or market.quote_asset, -delta
                    )
                snap["price"] = decimal_to_str(new_price)
                snap["quantity"] = decimal_to_str(new_quantity)
                snap["remaining_quantity"] = decimal_to_str(new_remaining)
                snap["sequence_number"] = int(sequencer_result.sequence_number)
                snap["updated_at"] = to_millis(now)
                snap["version"] = int(snap["version"] or 0) + 1
                reserve_aligned_before_fill = True
                try:
                    (
                        trade_payloads,
                        impacted,
                        _,
                        maker_pre_fill_anchors,
                    ) = await self._fast_apply_contract_fill_settlement(
                        session=session,
                        market=market,
                        taker_snap=snap,
                        result=result,
                        executed_at=now,
                    )
                except Exception as exc:
                    logging.getLogger("contract_service").warning(
                        "contract fast crossing amend settlement failed, restoring from DB: %s",
                        exc,
                    )
                    await self._fast_restore_market(
                        session, int(market.id), reason="contract_fast_amend_settlement_failed"
                    )
                    return None
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
                amend_kind = "cross"
                snap["remaining_quantity"] = decimal_to_str(result.remaining_quantity)
                if Decimal(snap["filled_quantity"]) == ZERO:
                    snap["status"] = ORDER_STATUS_NEW if result.placed_on_book else ORDER_STATUS_CANCELED
                elif Decimal(snap["remaining_quantity"]) == ZERO:
                    snap["status"] = ORDER_STATUS_FILLED
                else:
                    snap["status"] = ORDER_STATUS_PARTIALLY_FILLED
            else:
                sequencer_result = await self.runtime.get_symbol_sequencer(market.symbol).submit(
                    AmendOrderCommand(
                        order_id=order_id,
                        new_price=new_price,
                        new_remaining=new_remaining,
                        sequence_number=effective_command_sequence,
                    )
                )
                amend = sequencer_result.amend_result
                if amend is None:
                    evicted_ghost = self._evict_ephemeral_contract_ghost(
                        user=user,
                        market=market,
                        order_id=order_id,
                        expected_snap=snap,
                    )
                    logging.getLogger("contract_service").warning(
                        "fast simple amend skipped: engine amend returned None user_id=%s order_id=%s "
                        "new_price=%s new_remaining=%s evicted_ephemeral_ghost=%s",
                        user.id,
                        order_id,
                        new_price,
                        new_remaining,
                        evicted_ghost,
                    )
                    return None
                changed_bids = amend.changed_bids
                changed_asks = amend.changed_asks
                trade_payloads = []
                impacted = {int(snap["user_id"])}
                amend_kind = "simple"
                snap["status"] = ORDER_STATUS_PARTIALLY_FILLED if filled > ZERO else ORDER_STATUS_NEW

            if not reserve_aligned_before_fill:
                leverage = Decimal(snap.get("leverage") or market.default_leverage)
                old_reserve = old_price * old_remaining / leverage
                new_reserve = new_price * new_remaining / leverage
                delta = new_reserve - old_reserve
                if delta > ZERO:
                    try:
                        self.runtime.clearinghouse.reserve_contract_margin(
                            int(snap["user_id"]), market.margin_asset or market.quote_asset, delta
                        )
                    except ValueError as exc:
                        raise ContractValidationError(str(exc)) from exc
                elif delta < ZERO:
                    self.runtime.clearinghouse.release_contract_margin(
                        int(snap["user_id"]), market.margin_asset or market.quote_asset, -delta
                    )
                snap["price"] = decimal_to_str(new_price)
                snap["quantity"] = decimal_to_str(new_quantity)
                snap["remaining_quantity"] = decimal_to_str(new_remaining)
                snap["sequence_number"] = int(amend.sequence_number)
                snap["updated_at"] = to_millis(now)
                snap["version"] = int(snap["version"] or 0) + 1
            if snap["status"] in {ORDER_STATUS_FILLED, ORDER_STATUS_CANCELED}:
                self._fast_unregister_client_id(
                    int(snap["user_id"]), int(market.id), snap.get("client_order_id"), order_id
                )
                self._fast_contract_orders.pop(order_id, None)
            task = {
                "kind": "contract_amend",
                "symbol": market.symbol,
                "user_id": int(user.id),
                "order_id": order_id,
                "payload": payload.model_dump(mode="json") if hasattr(payload, "model_dump") else dict(payload),
                "old_remaining": decimal_to_str(old_remaining),
                "new_remaining": decimal_to_str(new_remaining),
                "sequence_number": int(snap["sequence_number"] or 0),
                "now": now.isoformat(),
                "amend_kind": amend_kind,
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
            await self._fast_broadcast_contract(
                market,
                changed_bids=changed_bids,
                changed_asks=changed_asks,
                trade_payloads=trade_payloads,
                order_snaps=[snap],
                impacted_users=impacted,
            )
        self._fast_metrics["fast_amend"] += 1
        self._fast_metrics["amend_success"] += 1
        response = {
            "order": self._fast_serialize_order(market, snap),
            "kept_priority": False if crossing_book else bool(amend.kept_priority),
            "trades": trade_payloads,
            "fast_path": True,
        }
        if not broadcast:
            response["_fast_flow"] = {
                "symbol": market.symbol,
                "changed_bids": changed_bids,
                "changed_asks": changed_asks,
                "trade_payloads": trade_payloads,
                "order_snaps": [snap],
                "impacted_users": impacted,
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
        if platform_durable_contract() or any(self._fast_contract_orders.get(item.order_id, {}).get("reserved_margin") is not None for item in payload.orders):
            return await self.amend_order_batch(session, user, payload)
        symbol = str(payload.symbol).upper()
        market = market_override or await self.get_market(session, symbol)
        book = bbo_snapshot
        if book is None:
            book, _, _ = await self.runtime.orderbook_snapshot(market.symbol, 1)
        items: list[dict] = []
        failed: list[dict] = []
        prepared: list[dict] = []
        batch_items: list[BatchAmendItem] = []
        pending_delta: dict[tuple[int, str], Decimal] = {}
        for index, item in enumerate(payload.orders):
            try:
                snap = self._fast_contract_orders.get(item.order_id)
                if snap is None:
                    durable = await session.scalar(select(Order).where(Order.order_id == item.order_id))
                    if (
                        durable is not None
                        and int(durable.user_id) == int(user.id)
                        and int(durable.market_id) == int(market.id)
                        and durable.product_type == PRODUCT_TYPE_PERP
                        and durable.status in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                        and Decimal(durable.remaining_quantity) > ZERO
                    ):
                        logging.getLogger("contract_service").error(
                            "fast batch amend unavailable: durable live order missing from mirror "
                            "user_id=%s order_id=%s",
                            user.id,
                            item.order_id,
                        )
                        return None
                    raise ContractValidationError("order not found on fast book")
                if snap["symbol"] != symbol:
                    raise ContractValidationError("order market mismatch")
                if int(snap["user_id"]) != int(user.id):
                    raise ContractValidationError("cannot amend others order")
                new_price = self._normalize_price(market, item.price if item.price is not None else snap["price"])
                new_quantity = self._normalize_qty(market, item.quantity)
                filled = Decimal(snap["filled_quantity"])
                new_remaining = self._normalize_qty(market, new_quantity - filled)
                old_price = Decimal(snap["price"]) if snap.get("price") else ZERO
                old_remaining = Decimal(snap["remaining_quantity"])
                if old_price <= ZERO or new_price is None or new_price <= ZERO:
                    raise ContractValidationError("limit order price missing")
                pseudo_order = SimpleNamespace(
                    order_id=item.order_id,
                    user_id=int(snap["user_id"]),
                    side=snap["side"],
                    position_action=snap["position_action"],
                    reduce_only=bool(snap.get("reduce_only")),
                    type=snap["type"],
                    tif=snap["tif"],
                    status=snap["status"],
                    sequence_number=int(snap.get("sequence_number") or 0),
                    version=int(snap.get("version") or 0),
                    price=decimal_to_str(old_price),
                    quantity=snap["quantity"],
                    filled_quantity=snap["filled_quantity"],
                    remaining_quantity=snap["remaining_quantity"],
                    leverage=snap.get("leverage"),
                )
                await self.validate_amend(session, user, market, pseudo_order, item, fast=True)
                best_ask = Decimal(book["asks"][0][0]) if book["asks"] else None
                best_bid = Decimal(book["bids"][0][0]) if book["bids"] else None
                crossing_book = (
                    (snap["side"] == SIDE_BUY and best_ask is not None and new_price >= best_ask)
                    or (snap["side"] == SIDE_SELL and best_bid is not None and new_price <= best_bid)
                )
                if crossing_book:
                    raise ContractValidationError("batch amend does not support crossing book")
                leverage = Decimal(snap.get("leverage") or market.default_leverage)
                old_reserve = old_price * old_remaining / leverage
                new_reserve = new_price * new_remaining / leverage
                delta = new_reserve - old_reserve
                if snap.get("position_action") == POSITION_ACTION_OPEN and delta > ZERO:
                    margin_asset = market.margin_asset or market.quote_asset
                    key = (int(snap["user_id"]), margin_asset)
                    available = self.runtime.clearinghouse.contract_available(int(snap["user_id"]), margin_asset)
                    if available - pending_delta.get(key, ZERO) < delta:
                        raise ContractValidationError("insufficient available margin")
                    pending_delta[key] = pending_delta.get(key, ZERO) + delta
                changed = not (
                    new_price == old_price
                    and new_quantity == Decimal(snap["quantity"])
                )
                entry = {
                    "index": index,
                    "snap": snap,
                    "new_price": new_price,
                    "new_quantity": new_quantity,
                    "new_remaining": new_remaining,
                    "old_price": old_price,
                    "old_remaining": old_remaining,
                    "filled": filled,
                    "delta": delta,
                    "changed": changed,
                }
                prepared.append(entry)
                if changed:
                    batch_items.append(
                        BatchAmendItem(order_id=item.order_id, new_price=new_price, new_remaining=new_remaining)
                    )
            except ContractValidationError as exc:
                failed.append(
                    {
                        "index": index,
                        "order_id": item.order_id,
                        "client_order_id": self._fast_contract_orders.get(item.order_id, {}).get("client_order_id"),
                        "error": str(exc),
                    }
                )
        async with self.runtime.fast_matching_guard(market.symbol):
            for entry in prepared:
                if not entry["changed"]:
                    continue
                snap = entry["snap"]
                leverage = Decimal(snap.get("leverage") or market.default_leverage)
                if entry["delta"] > ZERO:
                    try:
                        self.runtime.clearinghouse.reserve_contract_margin(
                            int(snap["user_id"]), market.margin_asset or market.quote_asset, entry["delta"]
                        )
                    except ValueError as exc:
                        raise ContractValidationError(str(exc)) from exc
                elif entry["delta"] < ZERO:
                    self.runtime.clearinghouse.release_contract_margin(
                        int(snap["user_id"]), market.margin_asset or market.quote_asset, -entry["delta"]
                    )
            if batch_items:
                sequencer_result = await self.runtime.get_symbol_sequencer(symbol).submit(BatchAmendCommand(batch_items))
                batch = sequencer_result.batch_amend_result
                if batch is None:
                    raise ContractValidationError("sequencer did not return batch amend result")
                for entry in prepared:
                    if not entry["changed"]:
                        continue
                    amend = batch.results.get(entry["snap"]["order_id"])
                    if amend is None:
                        raise ContractValidationError(f"orders not found on book: {entry['snap']['order_id']}")
                    snap = entry["snap"]
                    snap["price"] = decimal_to_str(entry["new_price"])
                    snap["quantity"] = decimal_to_str(entry["new_quantity"])
                    snap["remaining_quantity"] = decimal_to_str(entry["new_remaining"])
                    snap["sequence_number"] = int(amend.sequence_number or snap.get("sequence_number") or 0)
                    snap["updated_at"] = to_millis(datetime.now(tz=UTC))
                    snap["version"] = int(snap["version"] or 0) + 1
                    snap["status"] = ORDER_STATUS_PARTIALLY_FILLED if entry["filled"] > ZERO else ORDER_STATUS_NEW
                changed_bids = sequencer_result.changed_bids
                changed_asks = sequencer_result.changed_asks
            else:
                changed_bids = []
                changed_asks = []
            flows: list[dict] = []
            for entry in prepared:
                if not entry["changed"]:
                    item_result = {
                        "index": entry["index"],
                        "changed": False,
                        "fast_path": True,
                    }
                    if include_orders:
                        item_result["order"] = self._fast_serialize_order(market, entry["snap"])
                    items.append(item_result)
                    continue
                snap = entry["snap"]
                task = {
                    "kind": "contract_amend",
                    "symbol": market.symbol,
                    "user_id": int(user.id),
                    "order_id": snap["order_id"],
                    "payload": {
                        "quantity": decimal_to_str(entry["new_quantity"]),
                        "price": decimal_to_str(entry["new_price"]),
                    },
                    "new_remaining": decimal_to_str(entry["new_remaining"]),
                    "sequence_number": int(snap["sequence_number"] or 0),
                    "now": datetime.now(tz=UTC).isoformat(),
                    "amend_kind": "simple",
                    "engine_result": None,
                }
                # sampled mode keeps the live contract mirror in memory and
                # intentionally has no durable per-order sink. Do not pay a
                # writer enqueue/dict/metric cost for every level in a quote
                # batch; strict and memory modes retain their writer path.
                if True:
                    await self._fast_enqueue_or_restore(session, market, task)
                item_result = {
                    "index": entry["index"],
                    "changed": True,
                    "fast_path": True,
                }
                if include_orders:
                    item_result["order"] = self._fast_serialize_order(market, snap)
                items.append(item_result)
        if broadcast and (changed_bids or changed_asks):
            await self._fast_broadcast_contract(
                market,
                changed_bids=changed_bids,
                changed_asks=changed_asks,
                trade_payloads=[],
                order_snaps=[entry["snap"] for entry in prepared],
                impacted_users={int(user.id)},
            )
        self._fast_metrics["fast_batch_amend"] += 1
        self._fast_metrics["batch_amend_calls"] += 1
        self._fast_metrics["amend_success"] += sum(1 for entry in prepared if entry["changed"])
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
                "order_snaps": [entry["snap"] for entry in prepared],
                "impacted_users": {int(user.id)},
            }
        return response

    async def place_order_batch_fast(self, session: AsyncSession, user: User, payloads: list) -> dict:
        if not payloads:
            raise ContractValidationError("orders is required")
        symbols = {str(payload.symbol).upper() for payload in payloads}
        if len(symbols) != 1:
            raise ContractValidationError("all orders must belong to the same market")
        items: list[dict] = []
        failed: list[dict] = []
        flows: list[dict] = []
        market: Market | None = None
        for index, order_payload in enumerate(payloads):
            try:
                result = await self.place_order_fast(session, user, order_payload, broadcast=False)
                if result is None:
                    raise ContractValidationError("fast order path unavailable")
                if market is None:
                    market = await self.get_market(session, order_payload.symbol)
                flow = result.get("_fast_flow")
                if flow is not None:
                    flows.append(flow)
                items.append({"index": index, **result})
            except (ContractValidationError, ValueError) as exc:
                failed.append(
                    {
                        "index": index,
                        "client_order_id": order_payload.client_order_id,
                        "symbol": order_payload.symbol,
                        "error": str(exc),
                    }
                )
        if flows and market is not None:
            await self._fast_broadcast_contract(
                market,
                changed_bids=[item for flow in flows for item in flow["changed_bids"]],
                changed_asks=[item for flow in flows for item in flow["changed_asks"]],
                trade_payloads=[item for flow in flows for item in flow["trade_payloads"]],
                order_snaps=[snap for flow in flows for snap in flow.get("order_snaps", [])],
                impacted_users=set().union(*(flow.get("impacted_users") or set() for flow in flows)) if flows else set(),
            )
        self._fast_metrics["fast_batch_place"] += 1
        self._fast_metrics["batch_place_calls"] += 1
        return {
            "ok": not failed,
            "requested_count": len(payloads),
            "accepted_count": len(items),
            "failed_count": len(failed),
            "items": items,
            "failed": failed,
            "fast_path": True,
        }

    async def serialize_order(self, session: AsyncSession, order: Order, symbol: str | None = None, market: Market | None = None) -> dict:
        market_symbol = symbol
        if market_symbol is None:
            market = await session.scalar(select(Market).where(Market.id == order.market_id))
            if market is None:
                raise ContractValidationError("market not found")
            market_symbol = market.symbol
        if market is None:
            market = await session.scalar(select(Market).where(Market.symbol == market_symbol))
        assert market is not None
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
            "reject_reason": order.reject_reason,
            "created_at": to_millis(order.created_at),
            "updated_at": to_millis(order.updated_at),
        }

    async def serialize_trade(self, trade: Trade, symbol: str, market: Market) -> dict:
        return {
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "product_type": trade.product_type,
            "price": decimal_to_str(self._normalize_price(market, trade.price)),
            "quantity": decimal_to_str(self._normalize_qty(market, trade.quantity)),
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

    async def serialize_account_trade(self, trade: Trade, symbol: str, user_id: int, market: Market) -> dict:
        is_taker = trade.taker_user_id == user_id
        side = trade.taker_side if is_taker else (SIDE_SELL if trade.taker_side == SIDE_BUY else SIDE_BUY)
        return {
            "trade_id": trade.trade_id,
            "symbol": symbol,
            "product_type": trade.product_type,
            "side": side,
            "position_action": trade.taker_position_action if is_taker else trade.maker_position_action,
            "price": decimal_to_str(self._normalize_price(market, trade.price)),
            "quantity": decimal_to_str(self._normalize_qty(market, trade.quantity)),
            "quote_amount": decimal_to_str(quantize_scale(trade.quote_amount, 8)),
            "fee": decimal_to_str(quantize_scale(trade.taker_fee if is_taker else trade.maker_fee, 8)),
            "fee_asset": trade.fee_asset_taker if is_taker else trade.fee_asset_maker,
            "realized_pnl": decimal_to_str(quantize_scale(trade.taker_realized_pnl if is_taker else trade.maker_realized_pnl, 8)),
            "liquidity_role": "taker" if is_taker else "maker",
            "source": trade.source,
            "ts": to_millis(trade.executed_at),
        }

    def serialize_liquidation_event(self, event: ContractLiquidationEvent, market: Market) -> dict:
        return {
            "event_id": event.event_id,
            "symbol": market.symbol,
            "user_id": event.user_id,
            "position_side": event.position_side,
            "quantity": decimal_to_str(self._normalize_qty(market, event.quantity)),
            "entry_price": decimal_to_str(self._normalize_price(market, event.entry_price) or ZERO),
            "mark_price": decimal_to_str(self._normalize_price(market, event.mark_price) or ZERO),
            "liquidation_price": decimal_to_str(self._normalize_price(market, event.liquidation_price) or ZERO),
            "bankruptcy_price": decimal_to_str(self._normalize_price(market, event.bankruptcy_price) or ZERO),
            "realized_pnl": decimal_to_str(quantize_scale(event.realized_pnl, 8)),
            "released_margin": decimal_to_str(quantize_scale(event.released_margin, 8)),
            "insurance_covered": decimal_to_str(quantize_scale(event.insurance_covered, 8)),
            "residual_bad_debt": decimal_to_str(quantize_scale(event.residual_bad_debt, 8)),
            "adl_covered": decimal_to_str(quantize_scale(event.adl_covered, 8)),
            "adl_residual": decimal_to_str(quantize_scale(event.adl_residual, 8)),
            "adl_status": event.adl_status,
            "maintenance_margin": decimal_to_str(quantize_scale(event.maintenance_margin, 8)),
            "margin_buffer": decimal_to_str(quantize_scale(event.margin_buffer, 8)),
            "risk_status": event.risk_status,
            "reason": event.reason,
            "liquidated_at": to_millis(event.liquidated_at),
            "created_at": to_millis(event.created_at or event.liquidated_at),
        }

    async def serialize_account(self, session: AsyncSession, user_id: int, margin_asset: str = "USDT") -> dict:
        account = await self.get_account(session, user_id, margin_asset)
        return await self.serialize_existing_account(session, account)

    async def serialize_existing_account(self, session: AsyncSession, account: ContractAccount) -> dict:
        # Serialization is read-only. Risk maintenance and financial commands
        # own persisted mark-to-market updates and their ledger evidence.
        return {
            "user_id": account.user_id,
            "margin_asset": account.margin_asset,
            "wallet_balance": decimal_to_str(to_decimal(account.wallet_balance)),
            "available_margin": decimal_to_str(to_decimal(account.available_margin)),
            "used_margin": decimal_to_str(to_decimal(account.used_margin)),
            "unrealized_pnl": decimal_to_str(to_decimal(account.unrealized_pnl)),
            "realized_pnl": decimal_to_str(to_decimal(account.realized_pnl)),
            "total_fees": decimal_to_str(to_decimal(account.total_fees)),
            "updated_at": to_millis(account.updated_at),
        }

    async def serialize_position(self, position: ContractPosition, market: Market, session: AsyncSession | None = None) -> dict:
        # Live display calculations must not dirty the session's financial row.
        # Otherwise a later read can autoflush and retain a writer lock through
        # WebSocket delivery or the next maintenance market lock.
        position = SimpleNamespace(**{name: getattr(position, name) for name in (
            "side", "quantity", "entry_price", "mark_price", "liquidation_price",
            "leverage", "margin_mode", "isolated_margin", "maintenance_margin",
            "unrealized_pnl", "realized_pnl", "updated_at",
        )})
        await self.refresh_position(position, market, session)
        # Display normalization must never turn real positive exposure into zero.
        quantity = self._normalize_qty(market, position.quantity)
        if quantity == ZERO and to_decimal(position.quantity) > ZERO:
            quantity = to_decimal(position.quantity)
        risk = self.position_risk(position, market)
        notional = Decimal(position.mark_price) * Decimal(position.quantity)
        risk_tier = await self.risk_tier_for_notional(session, market, notional)
        return {
            "symbol": market.symbol,
            "product_type": market.product_type,
            "side": position.side,
            "quantity": decimal_to_str(quantity),
            "entry_price": decimal_to_str(self._normalize_price(market, position.entry_price) or ZERO),
            "mark_price": decimal_to_str(self._normalize_price(market, position.mark_price) or ZERO),
            "liquidation_price": decimal_to_str(self._normalize_price(market, position.liquidation_price) or ZERO),
            "leverage": decimal_to_str(to_decimal(position.leverage)),
            "margin_mode": position.margin_mode,
            "isolated_margin": decimal_to_str(to_decimal(position.isolated_margin)),
            "maintenance_margin": decimal_to_str(to_decimal(position.maintenance_margin)),
            "unrealized_pnl": decimal_to_str(to_decimal(position.unrealized_pnl)),
            "realized_pnl": decimal_to_str(to_decimal(position.realized_pnl)),
            "risk_status": risk["risk_status"],
            "liquidation_distance_pct": risk["liquidation_distance_pct"],
            "margin_buffer": risk["margin_buffer"],
            "risk_tier": int(risk_tier.tier),
            "risk_notional_floor": decimal_to_str(quantize_scale(risk_tier.notional_floor, 8)),
            "risk_notional_cap": decimal_to_str(quantize_scale(risk_tier.notional_cap, 8)) if risk_tier.notional_cap is not None else None,
            "risk_max_leverage": decimal_to_str(to_decimal(risk_tier.max_leverage)),
            "maintenance_margin_rate": decimal_to_str(to_decimal(risk_tier.maintenance_margin_rate)),
            "maintenance_amount": decimal_to_str(quantize_scale(to_decimal(risk_tier.maintenance_amount), 8)),
            "updated_at": to_millis(position.updated_at),
        }

    def position_risk(self, position: ContractPosition, market: Market) -> dict:
        qty = to_decimal(position.quantity)
        mark = to_decimal(position.mark_price)
        liquidation = to_decimal(position.liquidation_price)
        maintenance = to_decimal(position.maintenance_margin)
        margin_equity = to_decimal(position.isolated_margin) + to_decimal(position.unrealized_pnl)
        margin_buffer = margin_equity - maintenance
        distance_pct = ZERO
        if qty > ZERO and mark > ZERO and liquidation > ZERO:
            if position.side == POSITION_SIDE_LONG:
                distance_pct = (mark - liquidation) / mark
            elif position.side == POSITION_SIDE_SHORT:
                distance_pct = (liquidation - mark) / mark
        status = "flat"
        warning_distance = Decimal(str(settings.contract_risk_warning_distance_pct))
        watch_distance = Decimal(str(settings.contract_risk_watch_distance_pct))
        if qty > ZERO and position.side in {POSITION_SIDE_LONG, POSITION_SIDE_SHORT}:
            if distance_pct <= ZERO or margin_buffer <= ZERO:
                status = "liquidation_due"
            elif distance_pct <= warning_distance:
                status = "warning"
            elif distance_pct <= watch_distance:
                status = "watch"
            else:
                status = "ok"
        return {
            "risk_status": status,
            "liquidation_distance_pct": decimal_to_str(quantize_scale(distance_pct, 8)),
            "margin_buffer": decimal_to_str(quantize_scale(margin_buffer, 8)),
        }

    async def serialize_setting(self, setting: ContractUserSetting, market: Market) -> dict:
        return {
            "symbol": market.symbol,
            "leverage": decimal_to_str(to_decimal(setting.leverage)),
            "margin_mode": setting.margin_mode,
            "position_mode": setting.position_mode or POSITION_MODE_ONE_WAY,
            "max_leverage": decimal_to_str(to_decimal(market.max_leverage)),
            "default_leverage": decimal_to_str(to_decimal(market.default_leverage)),
        }

    async def broadcast_order_flow(
        self,
        session: AsyncSession,
        symbol: str,
        orders: list[Order],
        impacted_users: set[int],
        changed_bids: list[list[str]],
        changed_asks: list[list[str]],
        trade_payloads: list[dict],
    ) -> None:
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
        await self.runtime.ws.broadcast_public("stats", symbol, {"channel": "stats", "type": "update", "symbol": symbol, "data": stats})
        if trade_payloads:
            await self.runtime.ws.broadcast_public("trades", symbol, {"channel": "trades", "type": "update", "symbol": symbol, "items": trade_payloads})
            await self.runtime.market_data.broadcast_klines(self.runtime.ws, symbol)
        for user_id in impacted_users:
            for order in orders:
                if order.user_id == user_id:
                    await self.runtime.ws.broadcast_private(
                        user_id,
                        "orders",
                        {"channel": "orders", "type": "update", "data": await self.serialize_order(session, order, symbol)},
                    )
            if any(uid == int(user_id) for uid, _mid in self._ladder_bindings):
                continue
            account = await self.serialize_account(session, user_id)
            await self.runtime.ws.broadcast_private(
                user_id,
                "contracts",
                {"channel": "contracts", "type": "account", "data": account, "ts": to_millis(datetime.now(tz=UTC))},
            )
