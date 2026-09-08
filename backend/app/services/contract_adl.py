from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import POSITION_SIDE_FLAT, POSITION_SIDE_LONG, POSITION_SIDE_SHORT, ZERO
from app.core.decimal_utils import decimal_to_str, quantize_scale, quantize_step, to_decimal
from app.core.time_utils import to_millis
from app.models.contract_account import ContractAccount
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_position import ContractPosition
from app.models.market import Market
from app.models.user import User
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.ids import next_contract_adl_event_id

if TYPE_CHECKING:
    from app.services.contract_service import ContractService


ADL_EPSILON = Decimal("1e-10")


@dataclass(slots=True)
class ContractAdlCandidate:
    position: ContractPosition
    user: User
    pnl_pct: Decimal
    effective_leverage: Decimal
    rank_score: Decimal
    max_cover_amount: Decimal
    mark_price: Decimal


def adl_target_side(liquidation_side: str) -> str:
    if liquidation_side == POSITION_SIDE_LONG:
        return POSITION_SIDE_SHORT
    if liquidation_side == POSITION_SIDE_SHORT:
        return POSITION_SIDE_LONG
    raise ValueError("liquidation position side is not eligible for ADL")


def adl_coverage_delta(event: ContractLiquidationEvent) -> Decimal:
    mark = to_decimal(event.mark_price)
    bankruptcy = to_decimal(event.bankruptcy_price)
    if mark <= ZERO or bankruptcy <= ZERO:
        return ZERO
    if event.position_side == POSITION_SIDE_LONG:
        return max(bankruptcy - mark, ZERO)
    if event.position_side == POSITION_SIDE_SHORT:
        return max(mark - bankruptcy, ZERO)
    return ZERO


def adl_remaining(event: ContractLiquidationEvent) -> Decimal:
    residual = to_decimal(event.adl_residual)
    if residual > ZERO:
        return residual
    return max(to_decimal(event.residual_bad_debt) - to_decimal(event.adl_covered), ZERO)


def serialize_adl_candidate(candidate: ContractAdlCandidate, market: Market) -> dict:
    position = candidate.position
    return {
        "user": {"id": candidate.user.id, "username": candidate.user.username, "role": candidate.user.role},
        "symbol": market.symbol,
        "position_side": position.side,
        "quantity": decimal_to_str(quantize_scale(position.quantity, market.qty_precision)),
        "entry_price": decimal_to_str(quantize_scale(position.entry_price, market.price_precision)),
        "mark_price": decimal_to_str(quantize_scale(candidate.mark_price, market.price_precision)),
        "unrealized_pnl": decimal_to_str(quantize_scale(position.unrealized_pnl, 8)),
        "isolated_margin": decimal_to_str(quantize_scale(position.isolated_margin, 8)),
        "pnl_pct": decimal_to_str(quantize_scale(candidate.pnl_pct, 8)),
        "effective_leverage": decimal_to_str(quantize_scale(candidate.effective_leverage, 8)),
        "rank_score": decimal_to_str(quantize_scale(candidate.rank_score, 8)),
        "max_cover_amount": decimal_to_str(quantize_scale(candidate.max_cover_amount, 8)),
    }


def serialize_adl_event(event: ContractAdlEvent, symbol: str | None = None, username: str | None = None) -> dict:
    return {
        "event_id": event.event_id,
        "liquidation_event_id": event.liquidation_event_id,
        "user_id": event.user_id,
        "username": username,
        "market_id": event.market_id,
        "symbol": symbol,
        "position_side": event.position_side,
        "quantity": decimal_to_str(to_decimal(event.quantity)),
        "entry_price": decimal_to_str(to_decimal(event.entry_price)),
        "mark_price": decimal_to_str(to_decimal(event.mark_price)),
        "execution_price": decimal_to_str(to_decimal(event.execution_price)),
        "realized_pnl": decimal_to_str(to_decimal(event.realized_pnl)),
        "released_margin": decimal_to_str(to_decimal(event.released_margin)),
        "bad_debt_before": decimal_to_str(to_decimal(event.bad_debt_before)),
        "covered_amount": decimal_to_str(to_decimal(event.covered_amount)),
        "residual_after": decimal_to_str(to_decimal(event.residual_after)),
        "pnl_pct": decimal_to_str(to_decimal(event.pnl_pct)),
        "effective_leverage": decimal_to_str(to_decimal(event.effective_leverage)),
        "rank_score": decimal_to_str(to_decimal(event.rank_score)),
        "status": event.status,
        "reason": event.reason,
        "created_at": to_millis(event.created_at),
    }


async def build_contract_adl_candidates(
    session: AsyncSession,
    contract_service: ContractService,
    event: ContractLiquidationEvent,
    market: Market,
    *,
    limit: int = 20,
) -> list[ContractAdlCandidate]:
    delta = adl_coverage_delta(event)
    if delta <= ZERO:
        return []
    target_side = adl_target_side(event.position_side)
    rows = await session.execute(
        select(ContractPosition, User)
        .join(User, User.id == ContractPosition.user_id)
        .where(
            ContractPosition.market_id == market.id,
            ContractPosition.side == target_side,
            ContractPosition.quantity > ZERO,
        )
    )
    candidates: list[ContractAdlCandidate] = []
    for position, user in rows.all():
        await contract_service.refresh_position(position, market, session)
        qty = to_decimal(position.quantity)
        unrealized = to_decimal(position.unrealized_pnl)
        if qty <= ZERO or unrealized <= ZERO:
            continue
        mark = to_decimal(position.mark_price)
        isolated_margin = max(to_decimal(position.isolated_margin), ADL_EPSILON)
        margin_balance = max(isolated_margin + unrealized, ADL_EPSILON)
        notional = mark * qty
        pnl_pct = unrealized / isolated_margin
        effective_leverage = notional / margin_balance
        rank_score = pnl_pct * effective_leverage
        candidates.append(
            ContractAdlCandidate(
                position=position,
                user=user,
                pnl_pct=pnl_pct,
                effective_leverage=effective_leverage,
                rank_score=rank_score,
                max_cover_amount=delta * qty,
                mark_price=mark,
            )
        )
    candidates.sort(key=lambda item: (item.rank_score, item.effective_leverage, item.pnl_pct), reverse=True)
    return candidates[:limit]


async def execute_contract_adl(
    session: AsyncSession,
    contract_service: ContractService,
    event: ContractLiquidationEvent,
    market: Market,
    *,
    max_candidates: int = 20,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(tz=UTC)
    remaining = adl_remaining(event)
    if remaining <= ZERO:
        event.adl_residual = ZERO
        event.adl_status = "not_required" if to_decimal(event.residual_bad_debt) <= ZERO else "executed"
        await session.flush()
        return {"events": [], "covered_amount": "0", "residual_after": "0", "status": event.adl_status}

    delta = adl_coverage_delta(event)
    if delta <= ZERO:
        raise ValueError("ADL requires a positive bankruptcy/mark spread")

    candidates = await build_contract_adl_candidates(session, contract_service, event, market, limit=max_candidates)
    adl_events: list[ContractAdlEvent] = []
    execution_price = to_decimal(event.bankruptcy_price)
    total_covered = ZERO

    for candidate in candidates:
        if remaining <= ADL_EPSILON:
            break
        position = candidate.position
        qty = to_decimal(position.quantity)
        reduce_qty = min(qty, remaining / delta)
        reduce_qty = quantize_step(reduce_qty, to_decimal(market.qty_step))
        if reduce_qty <= ZERO:
            reduce_qty = min(qty, remaining / delta)
        reduce_qty = min(reduce_qty, qty)
        if reduce_qty <= ZERO:
            continue
        covered = min(remaining, reduce_qty * delta)
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == position.user_id,
                ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
            )
        )
        if account is None:
            continue
        before = snapshot_contract_account(account)
        old_qty = to_decimal(position.quantity)
        candidate_side = position.side
        close_ratio = reduce_qty / old_qty
        entry_price = to_decimal(position.entry_price)
        mark_price = to_decimal(position.mark_price)
        released_margin = to_decimal(position.isolated_margin) * close_ratio
        if position.side == POSITION_SIDE_LONG:
            realized = (execution_price - entry_price) * reduce_qty
        else:
            realized = (entry_price - execution_price) * reduce_qty

        event_id = next_contract_adl_event_id()
        bad_debt_before = remaining
        remaining = max(remaining - covered, ZERO)
        total_covered += covered

        position.quantity = old_qty - reduce_qty
        position.isolated_margin = to_decimal(position.isolated_margin) - released_margin
        position.realized_pnl = to_decimal(position.realized_pnl) + realized
        position_deleted = False
        if to_decimal(position.quantity) <= ADL_EPSILON:
            if await contract_service.is_hedge_market_maker(session, position.user_id, market):
                await session.delete(position)
                position_deleted = True
            else:
                position.side = POSITION_SIDE_FLAT
                position.quantity = ZERO
                position.entry_price = ZERO
                position.isolated_margin = ZERO
        position.updated_at = now
        if not position_deleted:
            await contract_service.refresh_position(position, market, session)

        account.wallet_balance = to_decimal(account.wallet_balance) + realized
        account.used_margin = max(to_decimal(account.used_margin) - released_margin, ZERO)
        account.realized_pnl = to_decimal(account.realized_pnl) + realized
        account.updated_at = now
        await contract_service.refresh_account(session, account)
        await add_contract_ledger_entry(
            session,
            account,
            change_type="adl_deleverage",
            amount=realized,
            before=before,
            market_id=market.id,
            related_event_id=event_id,
            note=f"adl:{event.event_id}",
            created_at=now,
        )

        adl_event = ContractAdlEvent(
            event_id=event_id,
            liquidation_event_id=event.event_id,
            user_id=position.user_id,
            market_id=market.id,
            position_side=candidate_side if candidate_side != POSITION_SIDE_FLAT else adl_target_side(event.position_side),
            quantity=reduce_qty,
            entry_price=entry_price,
            mark_price=mark_price,
            execution_price=execution_price,
            realized_pnl=realized,
            released_margin=released_margin,
            bad_debt_before=bad_debt_before,
            covered_amount=covered,
            residual_after=remaining,
            pnl_pct=candidate.pnl_pct,
            effective_leverage=candidate.effective_leverage,
            rank_score=candidate.rank_score,
            status="executed",
            reason="insurance_shortfall_adl",
            created_at=now,
        )
        session.add(adl_event)
        adl_events.append(adl_event)

    event.adl_covered = to_decimal(event.adl_covered) + total_covered
    event.adl_residual = remaining
    if remaining <= ADL_EPSILON:
        event.adl_residual = ZERO
        event.adl_status = "executed"
    elif total_covered > ZERO:
        event.adl_status = "partial"
    else:
        event.adl_status = "pending"
    await session.flush()

    return {
        "events": adl_events,
        "covered_amount": decimal_to_str(quantize_scale(total_covered, 8)),
        "residual_after": decimal_to_str(quantize_scale(event.adl_residual, 8)),
        "status": event.adl_status,
    }
