from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    FUNDING_RATE_MODE_BINANCE,
    FUNDING_RATE_MODE_FORMULA,
    INDEX_PRICE_SOURCE_BINANCE,
    MARK_PRICE_MODE_ORDERBOOK,
    POSITION_SIDE_LONG,
    POSITION_SIDE_SHORT,
    PRODUCT_TYPE_PERP,
    ZERO,
)
from app.core.decimal_utils import decimal_to_str, quantize_scale, to_decimal
from app.core.time_utils import to_millis
from app.models.contract_account import ContractAccount
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_funding_settlement import ContractFundingSettlement
from app.models.contract_market_state import ContractMarketState
from app.models.contract_position import ContractPosition
from app.models.market import Market
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.ids import next_funding_settlement_id, next_ledger_id
from app.services.runtime import AppRuntime


BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
DEFAULT_INDEX_PRICE = Decimal("0")
LOCAL_LAST_MAX_AGE_SECONDS = 60


@dataclass(slots=True)
class ExternalPremiumIndex:
    index_symbol: str
    index_price: Decimal
    external_mark_price: Decimal
    funding_rate: Decimal
    interest_rate: Decimal
    next_funding_time: datetime | None
    timestamp: datetime | None


class ContractPriceService:
    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def binance_index_symbol(market: Market) -> str:
        mapped = str(market.price_source_symbol or "").strip().upper()
        if mapped and str(market.price_source or "") == "binance":
            return mapped.removesuffix("-PERP")
        symbol = market.symbol.upper()
        if symbol.endswith("-PERP"):
            return symbol[:-5]
        return f"{market.base_asset}{market.quote_asset}".upper()

    @staticmethod
    def clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
        return min(max(value, lower), upper)

    @staticmethod
    def median_decimal(values: list[Decimal]) -> Decimal:
        cleaned = [value for value in values if value is not None and value > ZERO]
        if not cleaned:
            return ZERO
        return Decimal(str(median(cleaned)))

    @staticmethod
    def local_funding_boundary(market: Market, now: datetime) -> datetime:
        interval = max(1, int(market.funding_interval_hours or 8))
        hour = (now.hour // interval) * interval
        return now.replace(hour=hour, minute=0, second=0, microsecond=0)

    def resolve_funding_time(self, market: Market, funding_time: datetime | None) -> datetime:
        if funding_time is None:
            return self.local_funding_boundary(market, datetime.now(tz=UTC))
        if funding_time.tzinfo is None:
            return funding_time.replace(tzinfo=UTC)
        return funding_time.astimezone(UTC)

    async def get_state(self, session: AsyncSession, market: Market) -> ContractMarketState:
        state = await session.scalar(select(ContractMarketState).where(ContractMarketState.market_id == market.id))
        if state is None:
            state = ContractMarketState(
                market_id=market.id,
                index_symbol=self.binance_index_symbol(market),
                index_source=market.index_price_source,
                mark_source=market.mark_price_mode,
                funding_rate_mode=market.funding_rate_mode,
                interest_rate=market.funding_interest_rate,
                funding_clamp_rate=market.funding_clamp_rate,
                funding_cap_rate=market.funding_cap_rate,
                impact_notional=market.funding_impact_notional,
                index_price=market.reference_price or DEFAULT_INDEX_PRICE,
                mark_price=market.reference_price or DEFAULT_INDEX_PRICE,
            )
            session.add(state)
            await session.flush()
        return state

    async def _read_state_projection(self, session: AsyncSession, market: Market) -> ContractMarketState:
        """One detached display projection; GET requests never create state."""
        with session.no_autoflush:
            current = await session.scalar(
                select(ContractMarketState).where(ContractMarketState.market_id == market.id)
            )
        if current is not None:
            return ContractMarketState(**{
                column.key: getattr(current, column.key)
                for column in ContractMarketState.__table__.columns
            })
        return ContractMarketState(
            market_id=market.id,
            index_price=market.reference_price or ZERO,
            external_mark_price=ZERO,
            funding_rate=market.funding_rate or ZERO,
        )

    async def fetch_binance_premium_index(self, index_symbol: str) -> ExternalPremiumIndex:
        query = urllib.parse.urlencode({"symbol": index_symbol.upper()})
        url = f"{BINANCE_PREMIUM_INDEX_URL}?{query}"

        def load() -> dict:
            request = urllib.request.Request(url, headers={"User-Agent": "spot-mm-sandbox/contract-price"})
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = await asyncio.to_thread(load)
        next_funding = payload.get("nextFundingTime")
        ts = payload.get("time")
        return ExternalPremiumIndex(
            index_symbol=index_symbol.upper(),
            index_price=to_decimal(payload.get("indexPrice")),
            external_mark_price=to_decimal(payload.get("markPrice")),
            funding_rate=to_decimal(payload.get("lastFundingRate")),
            interest_rate=to_decimal(payload.get("interestRate")),
            next_funding_time=datetime.fromtimestamp(int(next_funding) / 1000, tz=UTC) if next_funding else None,
            timestamp=datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else None,
        )

    def local_last_price(self, symbol: str) -> Decimal:
        trades = self.runtime.market_data.recent_trade_items(symbol, 1)
        if not trades:
            return ZERO
        trade = trades[0]
        ts = int(trade.get("ts") or 0)
        if ts > 0:
            age_seconds = (to_millis(datetime.now(tz=UTC)) - ts) / 1000
            if age_seconds > LOCAL_LAST_MAX_AGE_SECONDS:
                return ZERO
        return to_decimal(trade["price"])

    def impact_price_for_notional(self, book: dict[str, list[list[str]]], side: str, target_notional: Decimal) -> Decimal:
        if target_notional <= ZERO:
            return ZERO
        levels = book["asks"] if side == "buy" else book["bids"]
        filled_notional = ZERO
        filled_qty = ZERO
        for price_text, qty_text in levels:
            price = to_decimal(price_text)
            qty = to_decimal(qty_text)
            if price <= ZERO or qty <= ZERO:
                continue
            level_notional = price * qty
            take_notional = min(level_notional, target_notional - filled_notional)
            filled_notional += take_notional
            filled_qty += take_notional / price
            if filled_notional >= target_notional:
                break
        if filled_qty <= ZERO:
            return ZERO
        return filled_notional / filled_qty

    async def local_book_prices(
        self, market: Market, index_price: Decimal, state: ContractMarketState,
        *, book_snapshot: dict[str, list[list[str]]] | None = None,
    ) -> dict[str, Decimal]:
        symbol = market.symbol
        if book_snapshot is None:
            book, _, _ = await self.runtime.orderbook_snapshot(symbol, 50)
        else:
            book = book_snapshot
        bids = book["bids"]
        asks = book["asks"]
        best_bid = to_decimal(bids[0][0]) if bids else ZERO
        best_ask = to_decimal(asks[0][0]) if asks else ZERO
        local_mid = ((best_bid + best_ask) / Decimal("2")) if best_bid > ZERO and best_ask > ZERO else best_bid or best_ask
        raw_local_last = self.local_last_price(symbol)
        impact_notional = Decimal(state.impact_notional or market.funding_impact_notional or ZERO)
        impact_bid = self.impact_price_for_notional(book, "sell", impact_notional) or best_bid or local_mid
        impact_ask = self.impact_price_for_notional(book, "buy", impact_notional) or best_ask or local_mid

        reference = index_price or to_decimal(market.reference_price) or local_mid or raw_local_last
        local_last = raw_local_last or local_mid or reference
        fair_local = self.median_decimal([best_bid, best_ask, raw_local_last or local_mid, local_mid])
        basis_source = local_mid or fair_local or reference
        if reference > ZERO and basis_source > ZERO:
            basis = (basis_source - reference) / reference
            limited_basis = self.clamp(basis, Decimal("-0.05"), Decimal("0.05"))
            basis_limited_price = reference * (Decimal("1") + limited_basis)
        else:
            basis_limited_price = basis_source or fair_local or reference
        mark = self.median_decimal([fair_local, local_mid, basis_limited_price])
        if mark <= ZERO:
            mark = reference
        premium = ZERO
        if reference > ZERO and impact_bid > ZERO and impact_ask > ZERO:
            premium = (max(impact_bid - reference, ZERO) - max(reference - impact_ask, ZERO)) / reference
        return {
            "local_mid_price": local_mid,
            "local_last_price": local_last,
            "impact_bid_price": impact_bid,
            "impact_ask_price": impact_ask,
            "premium_index": premium,
            "mark_price": quantize_scale(mark, market.price_precision),
            "index_price": reference,
        }

    def formula_funding_rate(self, premium_index: Decimal, market: Market, state: ContractMarketState) -> Decimal:
        interval_hours = Decimal(market.funding_interval_hours or 8)
        interest = Decimal(state.interest_rate or market.funding_interest_rate or Decimal("0.0001"))
        clamp_rate = Decimal(state.funding_clamp_rate or market.funding_clamp_rate or Decimal("0.0005"))
        cap_rate = Decimal(state.funding_cap_rate or market.funding_cap_rate or Decimal("0.02"))
        raw = premium_index + self.clamp(interest - premium_index, -clamp_rate, clamp_rate)
        if interval_hours > ZERO:
            raw = raw / (Decimal("8") / interval_hours)
        return self.clamp(raw, -cap_rate, cap_rate)

    async def refresh_market_state(
        self,
        session: AsyncSession,
        market: Market,
        *,
        fetch_external: bool = True,
        external_result: tuple[ExternalPremiumIndex | None, str, str] | None = None,
        persist: bool = True,
    ) -> ContractMarketState:
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ValueError("contract market state is only available for PERP markets")
        # get_state may INSERT a new row. Finish network I/O before that can
        # acquire SQLite's writer lock, including the first refresh of a market.
        external, source_status, source_message = (
            external_result if external_result is not None
            else await self._external_refresh(market, fetch_external=fetch_external)
        )
        # The cold snapshot fallback acquires the market lock. Resolve it before
        # get_state can start a write transaction for a newly added market.
        book_snapshot, _, _ = await self.runtime.orderbook_snapshot(market.symbol, 50)
        state = (
            await self.get_state(session, market) if persist
            else await self._read_state_projection(session, market)
        )
        state.index_symbol = self.binance_index_symbol(market)
        state.index_source = market.index_price_source
        state.mark_source = market.mark_price_mode
        state.funding_rate_mode = market.funding_rate_mode
        state.interest_rate = market.funding_interest_rate
        state.funding_clamp_rate = market.funding_clamp_rate
        state.funding_cap_rate = market.funding_cap_rate
        state.impact_notional = market.funding_impact_notional

        if external is not None:
            state.index_price = external.index_price
            state.external_mark_price = external.external_mark_price
            state.external_updated_at = external.timestamp or datetime.now(tz=UTC)
            state.next_funding_time = external.next_funding_time
            if external.interest_rate > ZERO:
                state.interest_rate = external.interest_rate

        if Decimal(state.index_price or ZERO) <= ZERO:
            state.index_price = market.reference_price or ZERO
        local = await self.local_book_prices(
            market, Decimal(state.index_price or ZERO), state, book_snapshot=book_snapshot
        )
        state.index_price = quantize_scale(local["index_price"], market.price_precision)
        state.mark_price = local["mark_price"]
        state.local_mid_price = local["local_mid_price"]
        state.local_last_price = local["local_last_price"]
        state.impact_bid_price = local["impact_bid_price"]
        state.impact_ask_price = local["impact_ask_price"]
        state.premium_index = local["premium_index"]
        if market.funding_rate_mode == FUNDING_RATE_MODE_BINANCE:
            if external is not None:
                state.funding_rate = self.clamp(external.funding_rate, -state.funding_cap_rate, state.funding_cap_rate)
        else:
            state.funding_rate = self.formula_funding_rate(Decimal(state.premium_index), market, state)
        state.source_status = source_status
        state.source_message = source_message
        state.updated_at = datetime.now(tz=UTC)
        # Fresh price observations keep their existing in-memory risk meaning.
        # A display GET must not depend on a SQL write to refresh the mark.
        self.runtime.contract_price_snapshots[market.symbol] = self.serialize_state(state, market)
        if persist:
            market.funding_rate = state.funding_rate
            now_ms = to_millis(state.updated_at)
            if now_ms - self.runtime.contract_price_persist_ms.get(market.symbol, 0) >= 1000:
                await session.flush()
                self.runtime.contract_price_persist_ms[market.symbol] = now_ms
        return state

    async def _external_refresh(
        self, market: Market, *, fetch_external: bool
    ) -> tuple[ExternalPremiumIndex | None, str, str]:
        if fetch_external and market.index_price_source == INDEX_PRICE_SOURCE_BINANCE:
            try:
                external = await self.fetch_binance_premium_index(self.binance_index_symbol(market))
                return external, "external_ok", "binance premiumIndex synced"
            except Exception as exc:
                return None, "fallback", f"binance premiumIndex unavailable: {exc.__class__.__name__}"
        return None, "fallback", "using local fallback"

    def serialize_state(self, state: ContractMarketState, market: Market) -> dict:
        return {
            "symbol": market.symbol,
            "product_type": market.product_type,
            "index_symbol": state.index_symbol,
            "index_source": state.index_source,
            "index_price": decimal_to_str(quantize_scale(state.index_price, market.price_precision)),
            "external_mark_price": decimal_to_str(quantize_scale(state.external_mark_price, market.price_precision)),
            "mark_price": decimal_to_str(quantize_scale(state.mark_price, market.price_precision)),
            "mark_source": state.mark_source,
            "local_mid_price": decimal_to_str(quantize_scale(state.local_mid_price, market.price_precision)),
            "local_last_price": decimal_to_str(quantize_scale(state.local_last_price, market.price_precision)),
            "impact_bid_price": decimal_to_str(quantize_scale(state.impact_bid_price, market.price_precision)),
            "impact_ask_price": decimal_to_str(quantize_scale(state.impact_ask_price, market.price_precision)),
            "premium_index": decimal_to_str(Decimal(state.premium_index)),
            "funding_rate": decimal_to_str(Decimal(state.funding_rate)),
            "funding_rate_mode": state.funding_rate_mode,
            "interest_rate": decimal_to_str(Decimal(state.interest_rate)),
            "funding_clamp_rate": decimal_to_str(Decimal(state.funding_clamp_rate)),
            "funding_cap_rate": decimal_to_str(Decimal(state.funding_cap_rate)),
            "impact_notional": decimal_to_str(Decimal(state.impact_notional)),
            "next_funding_time": to_millis(state.next_funding_time) if state.next_funding_time else None,
            "source_status": state.source_status,
            "source_message": state.source_message,
            "external_updated_at": to_millis(state.external_updated_at) if state.external_updated_at else None,
            "updated_at": to_millis(state.updated_at),
        }

    async def serialize_market_state(
        self,
        session: AsyncSession,
        market: Market,
        *,
        fetch_external: bool = False,
        persist: bool = True,
    ) -> dict:
        state = await self.refresh_market_state(session, market, fetch_external=fetch_external, persist=persist)
        return self.serialize_state(state, market)

    async def settle_funding(
        self,
        session: AsyncSession,
        market: Market,
        *,
        funding_time: datetime | None = None,
        fetch_external: bool = True,
    ) -> dict:
        external_result = await self._external_refresh(market, fetch_external=fetch_external)

        async def keys() -> list[str]:
            rows = await session.execute(
                select(ContractPosition.user_id).where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                ).distinct()
            )
            asset = (market.margin_asset or market.quote_asset).upper()
            return [f"contract:{int(user_id)}:{asset}" for user_id in sorted(rows.scalars())]

        async with self.runtime.market_financial_guard(market.symbol, keys):
            try:
                return await self._settle_funding_locked(
                    session,
                    market,
                    funding_time=funding_time,
                    fetch_external=fetch_external,
                    external_result=external_result,
                )
            except BaseException:
                # Do not expose the market/account locks while the failed
                # funding transaction still owns SQLite's writer lock.
                await session.rollback()
                raise

    async def _settle_funding_locked(
        self,
        session: AsyncSession,
        market: Market,
        *,
        funding_time: datetime | None = None,
        fetch_external: bool = True,
        external_result: tuple[ExternalPremiumIndex | None, str, str] | None = None,
    ) -> dict:
        state = await self.refresh_market_state(
            session, market, fetch_external=fetch_external, external_result=external_result
        )
        funding_time = self.resolve_funding_time(market, funding_time)
        settlement, acquired = await self.reserve_funding_settlement(session, market, state, funding_time)
        if not acquired:
            await session.commit()
            return {
                "symbol": market.symbol,
                "funding_time": to_millis(funding_time),
                "funding_rate": decimal_to_str(Decimal(settlement.funding_rate)) if settlement is not None else decimal_to_str(Decimal(state.funding_rate)),
                "settled_count": 0,
                "already_settled": True,
                "settlement_id": settlement.settlement_id if settlement is not None else None,
                "status": settlement.status if settlement is not None else "settled",
                "items": [],
            }
        rows = await session.execute(
            select(ContractPosition, ContractAccount)
            .join(ContractAccount, ContractAccount.user_id == ContractPosition.user_id)
            .where(
                ContractPosition.market_id == market.id,
                ContractPosition.quantity > ZERO,
                ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
            )
        )
        events: list[ContractFundingEvent] = []
        total_amount = ZERO
        for position, account in rows.all():
            notional = Decimal(position.quantity) * Decimal(state.index_price)
            charge = notional * Decimal(state.funding_rate)
            amount = -charge if position.side == POSITION_SIDE_LONG else charge
            if position.side not in {POSITION_SIDE_LONG, POSITION_SIDE_SHORT}:
                continue
            total_amount += amount
            before = snapshot_contract_account(account)
            account.wallet_balance = Decimal(account.wallet_balance) + amount
            account.available_margin = Decimal(account.available_margin) + amount
            account.updated_at = funding_time
            event_id = next_ledger_id().replace("l_", "f_")
            event = ContractFundingEvent(
                event_id=event_id,
                user_id=position.user_id,
                market_id=market.id,
                position_side=position.side,
                quantity=position.quantity,
                index_price=state.index_price,
                mark_price=state.mark_price,
                funding_rate=state.funding_rate,
                amount=amount,
                funding_rate_mode=state.funding_rate_mode,
                funding_time=funding_time,
            )
            session.add(event)
            events.append(event)
            await add_contract_ledger_entry(
                session,
                account,
                change_type="funding_fee",
                amount=amount,
                before=before,
                market_id=market.id,
                related_event_id=event_id,
                related_batch_id=settlement.settlement_id,
                note=f"funding:{state.funding_rate_mode}",
                created_at=funding_time,
            )
        settlement.status = "settled"
        settlement.settled_count = len(events)
        settlement.total_amount = total_amount
        settlement.completed_at = datetime.now(tz=UTC)
        settlement.updated_at = settlement.completed_at
        await session.flush()
        await session.commit()
        return {
            "symbol": market.symbol,
            "funding_time": to_millis(funding_time),
            "funding_rate": decimal_to_str(Decimal(state.funding_rate)),
            "settled_count": len(events),
            "already_settled": False,
            "settlement_id": settlement.settlement_id,
            "status": settlement.status,
            "items": [self.serialize_funding_event(event, market) for event in events],
        }

    async def reserve_funding_settlement(
        self,
        session: AsyncSession,
        market: Market,
        state: ContractMarketState,
        funding_time: datetime,
    ) -> tuple[ContractFundingSettlement | None, bool]:
        existing = await session.scalar(
            select(ContractFundingSettlement).where(
                ContractFundingSettlement.market_id == market.id,
                ContractFundingSettlement.funding_time == funding_time,
            )
        )
        if existing is not None:
            return existing, False

        settlement = ContractFundingSettlement(
            settlement_id=next_funding_settlement_id(),
            market_id=market.id,
            funding_time=funding_time,
            funding_rate=state.funding_rate,
            funding_rate_mode=state.funding_rate_mode,
            index_price=state.index_price,
            mark_price=state.mark_price,
            status="settling",
        )
        try:
            async with session.begin_nested():
                session.add(settlement)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(ContractFundingSettlement).where(
                    ContractFundingSettlement.market_id == market.id,
                    ContractFundingSettlement.funding_time == funding_time,
                )
            )
            return existing, False
        return settlement, True

    def serialize_funding_event(self, event: ContractFundingEvent, market: Market) -> dict:
        return {
            "event_id": event.event_id,
            "symbol": market.symbol,
            "user_id": event.user_id,
            "position_side": event.position_side,
            "quantity": decimal_to_str(Decimal(event.quantity)),
            "index_price": decimal_to_str(quantize_scale(event.index_price, market.price_precision)),
            "mark_price": decimal_to_str(quantize_scale(event.mark_price, market.price_precision)),
            "funding_rate": decimal_to_str(Decimal(event.funding_rate)),
            "amount": decimal_to_str(Decimal(event.amount)),
            "funding_rate_mode": event.funding_rate_mode,
            "funding_time": to_millis(event.funding_time),
            "created_at": to_millis(event.created_at),
        }

    def serialize_funding_settlement(self, settlement: ContractFundingSettlement, market: Market) -> dict:
        return {
            "settlement_id": settlement.settlement_id,
            "symbol": market.symbol,
            "funding_time": to_millis(settlement.funding_time),
            "funding_rate": decimal_to_str(Decimal(settlement.funding_rate)),
            "funding_rate_mode": settlement.funding_rate_mode,
            "index_price": decimal_to_str(quantize_scale(settlement.index_price, market.price_precision)),
            "mark_price": decimal_to_str(quantize_scale(settlement.mark_price, market.price_precision)),
            "settled_count": settlement.settled_count,
            "total_amount": decimal_to_str(Decimal(settlement.total_amount)),
            "status": settlement.status,
            "error_message": settlement.error_message,
            "started_at": to_millis(settlement.started_at),
            "completed_at": to_millis(settlement.completed_at) if settlement.completed_at else None,
            "created_at": to_millis(settlement.created_at),
            "updated_at": to_millis(settlement.updated_at),
        }
