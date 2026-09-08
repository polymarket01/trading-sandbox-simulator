from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ZERO
from app.core.decimal_utils import decimal_to_str, to_decimal
from app.core.time_utils import to_millis
from app.models.contract_insurance_event import ContractInsuranceEvent
from app.models.contract_insurance_fund import ContractInsuranceFund
from app.services.ids import next_contract_insurance_event_id
from app.services.accounting_service import shadow_insurance_event
from app.services.financial_outbox_service import enqueue_financial_outbox


async def get_contract_insurance_fund(session: AsyncSession, margin_asset: str = "USDT") -> ContractInsuranceFund:
    asset = margin_asset.upper().strip() or "USDT"
    fund = await session.scalar(select(ContractInsuranceFund).where(ContractInsuranceFund.margin_asset == asset))
    if fund is None:
        fund = ContractInsuranceFund(margin_asset=asset, balance=ZERO)
        session.add(fund)
        await session.flush()
    return fund


async def apply_contract_insurance_change(
    session: AsyncSession,
    *,
    margin_asset: str,
    event_type: str,
    amount: Decimal,
    residual_bad_debt: Decimal = ZERO,
    user_id: int | None = None,
    market_id: int | None = None,
    related_liquidation_event_id: str | None = None,
    note: str | None = None,
    created_at: datetime | None = None,
    allow_negative: bool = False,
) -> ContractInsuranceEvent:
    fund = await get_contract_insurance_fund(session, margin_asset)
    before = to_decimal(fund.balance)
    after = before + amount
    if after < ZERO and not allow_negative:
        raise ValueError("contract insurance fund balance would become negative")
    fund.balance = after
    fund.updated_at = created_at or datetime.now(tz=UTC)
    event = ContractInsuranceEvent(
        event_id=next_contract_insurance_event_id(),
        margin_asset=fund.margin_asset,
        event_type=event_type,
        amount=amount,
        balance_before=before,
        balance_after=after,
        residual_bad_debt=residual_bad_debt,
        user_id=user_id,
        market_id=market_id,
        related_liquidation_event_id=related_liquidation_event_id,
        note=note,
        created_at=created_at or datetime.now(tz=UTC),
    )
    session.add(event)
    await session.flush()
    accounting_transaction = await shadow_insurance_event(
        session,
        event_id=event.event_id,
        asset=event.margin_asset,
        event_type=event.event_type,
        amount=to_decimal(event.amount),
        residual_bad_debt=to_decimal(event.residual_bad_debt),
        effective_at=event.created_at,
        related_liquidation_event_id=event.related_liquidation_event_id,
    )
    await enqueue_financial_outbox(
        session,
        event_type="insurance_event_committed",
        account_domain="insurance",
        aggregate_type="insurance_fund",
        aggregate_id=event.margin_asset,
        idempotency_key=f"outbox:insurance-event:{event.event_id}",
        occurred_at=event.created_at,
        accounting_transaction_id=accounting_transaction.transaction_id,
        source_event_id=event.event_id,
        user_id=event.user_id,
        asset=event.margin_asset,
        payload={
            "insurance_event_id": event.event_id,
            "event_type": event.event_type,
            "amount": str(event.amount),
            "residual_bad_debt": str(event.residual_bad_debt),
            "related_liquidation_event_id": event.related_liquidation_event_id,
        },
    )
    return event


async def cover_contract_bad_debt(
    session: AsyncSession,
    *,
    margin_asset: str,
    bad_debt: Decimal,
    user_id: int,
    market_id: int,
    related_liquidation_event_id: str,
    note: str,
    created_at: datetime,
) -> tuple[Decimal, Decimal]:
    debt = max(to_decimal(bad_debt), ZERO)
    if debt <= ZERO:
        return ZERO, ZERO
    fund = await get_contract_insurance_fund(session, margin_asset)
    cover = min(to_decimal(fund.balance), debt)
    residual = debt - cover
    if cover > ZERO:
        await apply_contract_insurance_change(
            session,
            margin_asset=margin_asset,
            event_type="bad_debt_cover",
            amount=-cover,
            residual_bad_debt=residual,
            user_id=user_id,
            market_id=market_id,
            related_liquidation_event_id=related_liquidation_event_id,
            note=note,
            created_at=created_at,
        )
    if residual > ZERO:
        await apply_contract_insurance_change(
            session,
            margin_asset=margin_asset,
            event_type="bad_debt_uncovered",
            amount=ZERO,
            residual_bad_debt=residual,
            user_id=user_id,
            market_id=market_id,
            related_liquidation_event_id=related_liquidation_event_id,
            note="adl_pending",
            created_at=created_at,
        )
    return cover, residual


def serialize_contract_insurance_fund(fund: ContractInsuranceFund) -> dict:
    return {
        "margin_asset": fund.margin_asset,
        "balance": decimal_to_str(to_decimal(fund.balance)),
        "updated_at": to_millis(fund.updated_at),
    }


def serialize_contract_insurance_event(event: ContractInsuranceEvent, symbol: str | None = None) -> dict:
    return {
        "event_id": event.event_id,
        "margin_asset": event.margin_asset,
        "symbol": symbol,
        "event_type": event.event_type,
        "amount": decimal_to_str(to_decimal(event.amount)),
        "balance_before": decimal_to_str(to_decimal(event.balance_before)),
        "balance_after": decimal_to_str(to_decimal(event.balance_after)),
        "residual_bad_debt": decimal_to_str(to_decimal(event.residual_bad_debt)),
        "user_id": event.user_id,
        "market_id": event.market_id,
        "related_liquidation_event_id": event.related_liquidation_event_id,
        "note": event.note,
        "created_at": to_millis(event.created_at),
    }
