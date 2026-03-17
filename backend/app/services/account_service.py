from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ZERO
from app.models.balance import Balance
from app.models.ledger_entry import LedgerEntry
from app.models.reset_template import ResetTemplate
from app.services.ids import next_ledger_id

BALANCE_EPSILON = Decimal("1e-12")


class AccountService:
    async def get_balance(self, session: AsyncSession, user_id: int, asset: str) -> Balance:
        result = await session.execute(
            select(Balance).where(Balance.user_id == user_id, Balance.asset == asset)
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            balance = Balance(user_id=user_id, asset=asset, available=ZERO, frozen=ZERO)
            session.add(balance)
            await session.flush()
        return balance

    async def apply_change(
        self,
        session: AsyncSession,
        user_id: int,
        asset: str,
        available_delta: Decimal,
        frozen_delta: Decimal,
        change_type: str,
        *,
        related_order_id: str | None = None,
        related_trade_id: str | None = None,
        note: str | None = None,
        amount: Decimal | None = None,
        created_at: datetime | None = None,
    ) -> Balance:
        balance = await self.get_balance(session, user_id, asset)
        before_available = Decimal(balance.available)
        before_frozen = Decimal(balance.frozen)
        after_available = before_available + available_delta
        after_frozen = before_frozen + frozen_delta
        if -BALANCE_EPSILON < after_available < ZERO:
            after_available = ZERO
        if -BALANCE_EPSILON < after_frozen < ZERO:
            after_frozen = ZERO
        if after_available < ZERO or after_frozen < ZERO:
            raise ValueError(f"Balance would become negative for user={user_id} asset={asset}")
        balance.available = after_available
        balance.frozen = after_frozen
        balance.updated_at = created_at or datetime.now(timezone.utc)
        total_before = before_available + before_frozen
        total_after = after_available + after_frozen
        entry = LedgerEntry(
            entry_id=next_ledger_id(),
            user_id=user_id,
            asset=asset,
            change_type=change_type,
            amount=amount if amount is not None else total_after - total_before,
            balance_before=total_before,
            balance_after=total_after,
            available_before=before_available,
            available_after=after_available,
            frozen_before=before_frozen,
            frozen_after=after_frozen,
            related_order_id=related_order_id,
            related_trade_id=related_trade_id,
            note=note,
            created_at=created_at or datetime.now(timezone.utc),
        )
        session.add(entry)
        await session.flush()
        return balance

    async def ensure_available(self, session: AsyncSession, user_id: int, asset: str, needed: Decimal) -> None:
        balance = await self.get_balance(session, user_id, asset)
        if Decimal(balance.available) < needed:
            raise ValueError(f"Insufficient balance for asset {asset}")

    async def reserve(
        self,
        session: AsyncSession,
        user_id: int,
        asset: str,
        amount: Decimal,
        *,
        related_order_id: str,
        note: str,
        created_at: datetime,
    ) -> None:
        if amount <= ZERO:
            return
        await self.ensure_available(session, user_id, asset, amount)
        await self.apply_change(
            session,
            user_id,
            asset,
            available_delta=-amount,
            frozen_delta=amount,
            change_type="freeze",
            related_order_id=related_order_id,
            note=note,
            amount=ZERO,
            created_at=created_at,
        )

    async def release(
        self,
        session: AsyncSession,
        user_id: int,
        asset: str,
        amount: Decimal,
        *,
        related_order_id: str,
        note: str,
        created_at: datetime,
    ) -> None:
        if amount <= ZERO:
            return
        await self.apply_change(
            session,
            user_id,
            asset,
            available_delta=amount,
            frozen_delta=-amount,
            change_type="unfreeze",
            related_order_id=related_order_id,
            note=note,
            amount=ZERO,
            created_at=created_at,
        )

    async def reset_balances(self, session: AsyncSession, user_id: int, template_name: str) -> None:
        result = await session.execute(
            select(ResetTemplate).where(ResetTemplate.user_id == user_id, ResetTemplate.name == template_name)
        )
        templates = {template.asset: Decimal(template.amount) for template in result.scalars()}
        balances_result = await session.execute(select(Balance).where(Balance.user_id == user_id))
        existing_assets = {balance.asset for balance in balances_result.scalars()}
        now = datetime.now(timezone.utc)
        for asset in sorted(set(templates) | existing_assets):
            balance = await self.get_balance(session, user_id, asset)
            target_available = templates.get(asset, ZERO)
            before_available = Decimal(balance.available)
            before_frozen = Decimal(balance.frozen)
            available_delta = target_available - before_available
            frozen_delta = ZERO - before_frozen
            if available_delta == ZERO and frozen_delta == ZERO:
                continue
            await self.apply_change(
                session,
                user_id,
                asset,
                available_delta=available_delta,
                frozen_delta=frozen_delta,
                change_type="balance_reset",
                note=f"reset:{template_name}",
                amount=target_available - (before_available + before_frozen),
                created_at=now,
            )
        await session.flush()
