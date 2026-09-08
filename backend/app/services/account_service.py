from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ZERO
from app.models.balance import Balance
from app.models.ledger_entry import LedgerEntry
from app.models.reset_template import ResetTemplate
from app.models.user import User
from app.services.ids import next_ledger_id
from app.services.accounting_service import shadow_spot_ledger_entry
from app.services.financial_outbox_service import enqueue_financial_outbox

BALANCE_EPSILON = Decimal("1e-12")
# SQLite stores the legacy NUMERIC balance columns with REAL affinity.  A
# 100-level quote ladder can therefore accumulate several 1e-8 of binary-float
# drift even when every individual reserve is step-aligned.  Release only the
# actually frozen amount inside this dust window; this never creates funds.
RELEASE_ROUNDING_EPSILON = Decimal("1e-7")


class AccountService:
    async def get_balance(self, session: AsyncSession, user_id: int, asset: str) -> Balance:
        result = await session.execute(
            select(Balance).where(Balance.user_id == user_id, Balance.asset == asset)
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            account_run_id = await session.scalar(select(User.current_account_run_id).where(User.id == user_id))
            balance = Balance(user_id=user_id, asset=asset, available=ZERO, frozen=ZERO, account_run_id=account_run_id)
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
        if available_delta == ZERO and frozen_delta == ZERO:
            return balance
        account_run_id = await session.scalar(select(User.current_account_run_id).where(User.id == user_id))
        if account_run_id:
            balance.account_run_id = account_run_id
        before_available = Decimal(balance.available)
        before_frozen = Decimal(balance.frozen)
        after_available = before_available + available_delta
        after_frozen = before_frozen + frozen_delta
        # SQLite 旧 NUMERIC 列走 REAL 亲和，100 档报价梯的多次 reserve/settle
        # 会累积数 1e-8 的二进制浮点尘埃（见 RELEASE_ROUNDING_EPSILON 的
        # 说明）。低于 1e-7 的负余额只是表示尘埃，钳到 0；真实透支（≥1e-7）
        # 仍然 fail-closed。这与 release 侧已有的宽容口径一致，不会凭空造钱。
        if -RELEASE_ROUNDING_EPSILON < after_available < ZERO:
            after_available = ZERO
        if -RELEASE_ROUNDING_EPSILON < after_frozen < ZERO:
            after_frozen = ZERO
        if after_available < ZERO or after_frozen < ZERO:
            raise ValueError(f"Balance would become negative for user={user_id} asset={asset}")
        # Never discard a financial delta merely because a large SQLite REAL
        # balance makes the relative change look small. A 0.0001 BTC fill
        # against a large demo wallet is still a real trade leg and must have
        # its own ledger/shadow/outbox evidence. Only the explicit negative
        # dust clamp above normalizes representation noise.
        balance.available = after_available
        balance.frozen = after_frozen
        balance.updated_at = created_at or datetime.now(timezone.utc)
        total_before = before_available + before_frozen
        total_after = after_available + after_frozen
        entry_id = next_ledger_id()
        entry = LedgerEntry(
            entry_id=entry_id,
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
            account_run_id=account_run_id,
            note=note,
            created_at=created_at or datetime.now(timezone.utc),
        )
        session.add(entry)
        await session.flush()
        # SQLite NUMERIC uses REAL affinity for these legacy columns. Read the
        # committed representation back before creating the shadow movement so
        # the business ledger delta and accounting source fact are identical.
        await session.refresh(entry)
        persisted_available_delta = Decimal(entry.available_after) - Decimal(entry.available_before)
        persisted_frozen_delta = Decimal(entry.frozen_after) - Decimal(entry.frozen_before)
        if persisted_available_delta == ZERO and persisted_frozen_delta == ZERO:
            raise ValueError(
                f"Balance change is below persisted SQLite precision for user={user_id} asset={asset} "
                f"available_delta={available_delta} frozen_delta={frozen_delta} change_type={change_type}"
            )
        accounting_transaction = await shadow_spot_ledger_entry(
            session,
            legacy_entry_id=entry_id,
            user_id=user_id,
            asset=asset,
            event_type=change_type,
            available_delta=persisted_available_delta,
            frozen_delta=persisted_frozen_delta,
            effective_at=entry.created_at,
            source_order_id=related_order_id,
            source_trade_id=related_trade_id,
        )
        await enqueue_financial_outbox(
            session,
            event_type="spot_ledger_committed",
            account_domain="spot",
            aggregate_type="balance",
            aggregate_id=f"{user_id}:{asset}",
            idempotency_key=f"outbox:spot-ledger:{entry_id}",
            occurred_at=entry.created_at,
            accounting_transaction_id=accounting_transaction.transaction_id,
            source_ledger_entry_id=entry_id,
            user_id=user_id,
            asset=asset,
            payload={
                "ledger_entry_id": entry_id,
                "change_type": change_type,
                "available_delta": str(persisted_available_delta),
                "frozen_delta": str(persisted_frozen_delta),
                "related_order_id": related_order_id,
                "related_trade_id": related_trade_id,
            },
        )
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
        release_amount = amount
        balance = await self.get_balance(session, user_id, asset)
        frozen = Decimal(balance.frozen)
        if release_amount > frozen and release_amount - frozen <= RELEASE_ROUNDING_EPSILON:
            # SQLite Numeric values can drift by tiny sub-satoshi amounts after
            # many amend/release cycles. Do not mint that dust into available.
            release_amount = frozen
        if release_amount <= ZERO:
            return
        await self.apply_change(
            session,
            user_id,
            asset,
            available_delta=release_amount,
            frozen_delta=-release_amount,
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
