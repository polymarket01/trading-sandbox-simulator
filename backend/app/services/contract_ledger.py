from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.decimal_utils import decimal_to_str, to_decimal
from app.core.time_utils import to_millis
from app.models.contract_account import ContractAccount
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.services.ids import next_contract_ledger_id
from app.services.accounting_service import shadow_contract_ledger_entry
from app.services.financial_outbox_service import enqueue_financial_outbox

ContractAccountSnapshot = dict[str, Decimal]


def snapshot_contract_account(account: ContractAccount) -> ContractAccountSnapshot:
    return {
        "wallet": to_decimal(account.wallet_balance),
        "available": to_decimal(account.available_margin),
        "used_margin": to_decimal(account.used_margin),
        "unrealized_pnl": to_decimal(account.unrealized_pnl),
        "realized_pnl": to_decimal(account.realized_pnl),
        "total_fees": to_decimal(account.total_fees),
    }


def contract_account_snapshot_changed(before: ContractAccountSnapshot, after: ContractAccountSnapshot) -> bool:
    return any(before[key] != after[key] for key in before)


async def add_contract_ledger_entry(
    session: AsyncSession,
    account: ContractAccount,
    *,
    change_type: str,
    amount: Decimal,
    before: ContractAccountSnapshot,
    market_id: int | None = None,
    related_order_id: str | None = None,
    related_trade_id: str | None = None,
    related_event_id: str | None = None,
    related_batch_id: str | None = None,
    note: str | None = None,
    created_at: datetime | None = None,
    skip_if_unchanged: bool = True,
) -> ContractLedgerEntry | None:
    after = snapshot_contract_account(account)
    if skip_if_unchanged and not contract_account_snapshot_changed(before, after):
        return None
    entry_id = next_contract_ledger_id()
    entry = ContractLedgerEntry(
        entry_id=entry_id,
        user_id=account.user_id,
        account_run_id=getattr(account, "account_run_id", None),
        market_id=market_id,
        margin_asset=account.margin_asset,
        change_type=change_type,
        amount=amount,
        wallet_before=before["wallet"],
        wallet_after=after["wallet"],
        available_before=before["available"],
        available_after=after["available"],
        used_margin_before=before["used_margin"],
        used_margin_after=after["used_margin"],
        unrealized_pnl_before=before["unrealized_pnl"],
        unrealized_pnl_after=after["unrealized_pnl"],
        realized_pnl_before=before["realized_pnl"],
        realized_pnl_after=after["realized_pnl"],
        total_fees_before=before["total_fees"],
        total_fees_after=after["total_fees"],
        related_order_id=related_order_id,
        related_trade_id=related_trade_id,
        related_event_id=related_event_id,
        note=note,
        created_at=created_at or datetime.now(tz=UTC),
    )
    session.add(entry)
    await session.flush()
    # Use the exact values read back from the legacy SQLite representation.
    # This prevents the shadow source transaction from recording the pre-bind
    # Decimal while the business ledger stores a rounded REAL value.
    await session.refresh(entry)
    persisted_before = {
        "wallet": Decimal(entry.wallet_before),
        "available": Decimal(entry.available_before),
        "used_margin": Decimal(entry.used_margin_before),
        "unrealized_pnl": Decimal(entry.unrealized_pnl_before),
        "realized_pnl": Decimal(entry.realized_pnl_before),
        "total_fees": Decimal(entry.total_fees_before),
    }
    persisted_after = {
        "wallet": Decimal(entry.wallet_after),
        "available": Decimal(entry.available_after),
        "used_margin": Decimal(entry.used_margin_after),
        "unrealized_pnl": Decimal(entry.unrealized_pnl_after),
        "realized_pnl": Decimal(entry.realized_pnl_after),
        "total_fees": Decimal(entry.total_fees_after),
    }
    if not contract_account_snapshot_changed(persisted_before, persisted_after):
        await session.delete(entry)
        await session.flush()
        return None
    accounting_transaction = await shadow_contract_ledger_entry(
        session,
        legacy_entry_id=entry_id,
        user_id=account.user_id,
        asset=account.margin_asset,
        event_type=change_type,
        before=persisted_before,
        after=persisted_after,
        effective_at=entry.created_at,
        source_order_id=related_order_id,
        source_trade_id=related_trade_id,
        source_event_id=related_event_id,
        source_batch_id=related_batch_id,
    )
    await enqueue_financial_outbox(
        session,
        event_type="contract_ledger_committed",
        account_domain="contract",
        aggregate_type="contract_account",
        aggregate_id=f"{account.user_id}:{account.margin_asset}",
        idempotency_key=f"outbox:contract-ledger:{entry_id}",
        occurred_at=entry.created_at,
        accounting_transaction_id=accounting_transaction.transaction_id,
        source_ledger_entry_id=entry_id,
        source_event_id=related_event_id,
        user_id=account.user_id,
        asset=account.margin_asset,
        payload={
            "contract_ledger_entry_id": entry_id,
            "change_type": change_type,
            "related_order_id": related_order_id,
            "related_trade_id": related_trade_id,
            "related_event_id": related_event_id,
            "related_batch_id": related_batch_id,
        },
    )
    return entry


def serialize_contract_ledger_entry(entry: ContractLedgerEntry, symbol: str | None = None) -> dict:
    return {
        "entry_id": entry.entry_id,
        "user_id": entry.user_id,
        "symbol": symbol,
        "margin_asset": entry.margin_asset,
        "change_type": entry.change_type,
        "amount": decimal_to_str(to_decimal(entry.amount)),
        "wallet_before": decimal_to_str(to_decimal(entry.wallet_before)),
        "wallet_after": decimal_to_str(to_decimal(entry.wallet_after)),
        "available_before": decimal_to_str(to_decimal(entry.available_before)),
        "available_after": decimal_to_str(to_decimal(entry.available_after)),
        "used_margin_before": decimal_to_str(to_decimal(entry.used_margin_before)),
        "used_margin_after": decimal_to_str(to_decimal(entry.used_margin_after)),
        "unrealized_pnl_before": decimal_to_str(to_decimal(entry.unrealized_pnl_before)),
        "unrealized_pnl_after": decimal_to_str(to_decimal(entry.unrealized_pnl_after)),
        "realized_pnl_before": decimal_to_str(to_decimal(entry.realized_pnl_before)),
        "realized_pnl_after": decimal_to_str(to_decimal(entry.realized_pnl_after)),
        "total_fees_before": decimal_to_str(to_decimal(entry.total_fees_before)),
        "total_fees_after": decimal_to_str(to_decimal(entry.total_fees_after)),
        "related_order_id": entry.related_order_id,
        "related_trade_id": entry.related_trade_id,
        "related_event_id": entry.related_event_id,
        "note": entry.note,
        "created_at": to_millis(entry.created_at),
    }
