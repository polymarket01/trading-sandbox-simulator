from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import AccountingEntry, AccountingTransaction
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_insurance_event import ContractInsuranceEvent
from app.models.contract_insurance_fund import ContractInsuranceFund
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.ledger_entry import LedgerEntry
from app.models.reconciliation_run import ReconciliationRun


ZERO = Decimal("0")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _line(
    transaction_id: str,
    *,
    domain: str,
    owner_type: str,
    owner_id: str,
    asset: str,
    account_code: str,
    amount: Decimal,
    debit_when_positive: bool,
    created_at: datetime,
) -> AccountingEntry | None:
    amount = Decimal(amount)
    if amount == ZERO:
        return None
    positive = amount.copy_abs()
    debit = positive if (amount > ZERO) == debit_when_positive else ZERO
    credit = positive if debit == ZERO else ZERO
    return AccountingEntry(
        entry_id=_id("ae"),
        transaction_id=transaction_id,
        account_domain=domain,
        owner_type=owner_type,
        owner_id=owner_id,
        asset=asset,
        account_code=account_code,
        debit=debit,
        credit=credit,
        quantity=amount,
        created_at=created_at,
    )


async def _commit_shadow(
    session: AsyncSession,
    *,
    event_type: str,
    idempotency_key: str,
    entries: list[AccountingEntry | None],
    effective_at: datetime,
    source_order_id: str | None = None,
    source_trade_id: str | None = None,
    source_event_id: str | None = None,
    source_batch_id: str | None = None,
    source_ledger_entry_id: str | None = None,
    reversal_of_transaction_id: str | None = None,
    metadata: dict | None = None,
    schema_version: str = "2",
) -> AccountingTransaction:
    existing = await session.scalar(
        select(AccountingTransaction).where(AccountingTransaction.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    rows = [entry for entry in entries if entry is not None]
    if not rows:
        raise ValueError("accounting transaction cannot be empty")
    debit = sum((Decimal(entry.debit) for entry in rows), ZERO)
    credit = sum((Decimal(entry.credit) for entry in rows), ZERO)
    if debit != credit:
        raise ValueError(f"unbalanced accounting transaction: debit={debit} credit={credit}")
    assets = {entry.asset for entry in rows}
    for asset in assets:
        asset_debit = sum((Decimal(entry.debit) for entry in rows if entry.asset == asset), ZERO)
        asset_credit = sum((Decimal(entry.credit) for entry in rows if entry.asset == asset), ZERO)
        if asset_debit != asset_credit:
            raise ValueError(
                f"unbalanced accounting transaction for asset={asset}: "
                f"debit={asset_debit} credit={asset_credit}"
            )
    transaction_id = rows[0].transaction_id
    transaction = AccountingTransaction(
        transaction_id=transaction_id,
        event_type=event_type,
        idempotency_key=idempotency_key,
        source_order_id=source_order_id,
        source_trade_id=source_trade_id,
        source_event_id=source_event_id,
        source_batch_id=source_batch_id,
        source_ledger_entry_id=source_ledger_entry_id,
        reversal_of_transaction_id=reversal_of_transaction_id,
        status="committed",
        effective_at=effective_at,
        created_at=datetime.now(tz=UTC),
        metadata_json=metadata or {},
        schema_version=schema_version,
    )
    session.add(transaction)
    # Flush the parent first. SQLite foreign_keys=ON is intentionally enabled;
    # relying on unit-of-work batch ordering here caused entries to race their
    # transaction during bootstrap executemany.
    await session.flush()
    session.add_all(rows)
    await session.flush()
    return transaction


async def shadow_spot_ledger_entry(
    session: AsyncSession,
    *,
    legacy_entry_id: str,
    user_id: int,
    asset: str,
    event_type: str,
    available_delta: Decimal,
    frozen_delta: Decimal,
    effective_at: datetime,
    source_order_id: str | None,
    source_trade_id: str | None,
    source_batch_id: str | None = None,
    evidence_metadata: dict | None = None,
) -> AccountingTransaction:
    transaction_id = _id("at")
    net = Decimal(available_delta) + Decimal(frozen_delta)
    entries = [
        _line(transaction_id, domain="spot", owner_type="customer", owner_id=str(user_id), asset=asset,
              account_code="customer_spot_available", amount=available_delta, debit_when_positive=True, created_at=effective_at),
        _line(transaction_id, domain="spot", owner_type="customer", owner_id=str(user_id), asset=asset,
              account_code="customer_spot_frozen", amount=frozen_delta, debit_when_positive=True, created_at=effective_at),
        _line(transaction_id, domain="spot", owner_type="system", owner_id="exchange", asset=asset,
              account_code="system_spot_clearing", amount=net, debit_when_positive=False, created_at=effective_at),
    ]
    return await _commit_shadow(
        session,
        event_type=event_type,
        idempotency_key=f"spot-ledger:{legacy_entry_id}",
        entries=entries,
        effective_at=effective_at,
        source_order_id=source_order_id,
        source_trade_id=source_trade_id,
        source_batch_id=source_batch_id,
        source_ledger_entry_id=legacy_entry_id,
        metadata={"legacy_ledger_entry_id": legacy_entry_id, **(evidence_metadata or {})},
    )


def _contract_offset_code(event_type: str) -> str:
    if event_type == "trade_fee":
        return "fee_income"
    if event_type == "funding_fee":
        return "funding_receivable_payable"
    if event_type in {"position_close", "liquidation", "adl_deleverage"}:
        return "realized_pnl_clearing"
    if event_type in {"account_init", "admin_adjust"}:
        return "system_init_adjustment"
    return "contract_wallet_clearing"


def _dimension_pair(
    transaction_id: str,
    *,
    owner_id: str,
    asset: str,
    customer_code: str,
    system_code: str,
    amount: Decimal,
    created_at: datetime,
    domain: str = "contract",
    customer_owner_type: str = "customer",
) -> list[AccountingEntry | None]:
    return [
        _line(
            transaction_id, domain=domain, owner_type=customer_owner_type, owner_id=owner_id,
            asset=asset, account_code=customer_code, amount=amount,
            debit_when_positive=True, created_at=created_at,
        ),
        _line(
            transaction_id, domain=domain, owner_type="system", owner_id="exchange",
            asset=asset, account_code=system_code, amount=amount,
            debit_when_positive=False, created_at=created_at,
        ),
    ]


async def shadow_contract_ledger_entry(
    session: AsyncSession,
    *,
    legacy_entry_id: str,
    user_id: int,
    asset: str,
    event_type: str,
    before: dict[str, Decimal],
    after: dict[str, Decimal],
    effective_at: datetime,
    source_order_id: str | None,
    source_trade_id: str | None,
    source_event_id: str | None,
    source_batch_id: str | None = None,
    evidence_metadata: dict | None = None,
) -> AccountingTransaction:
    transaction_id = _id("at")
    wallet_delta = after["wallet"] - before["wallet"]
    available_delta = after["available"] - before["available"]
    used_delta = after["used_margin"] - before["used_margin"]
    unrealized_delta = after["unrealized_pnl"] - before["unrealized_pnl"]
    realized_delta = after["realized_pnl"] - before["realized_pnl"]
    fees_delta = after["total_fees"] - before["total_fees"]
    entries: list[AccountingEntry | None] = []
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_contract_wallet", system_code=_contract_offset_code(event_type),
        amount=wallet_delta, created_at=effective_at,
    ))
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_contract_available", system_code="contract_available_control",
        amount=available_delta, created_at=effective_at,
    ))
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_margin_used", system_code="margin_used_control",
        amount=used_delta, created_at=effective_at,
    ))
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_contract_unrealized_pnl", system_code="unrealized_pnl_clearing",
        amount=unrealized_delta, created_at=effective_at,
    ))
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_contract_realized_pnl", system_code="realized_pnl_control",
        amount=realized_delta, created_at=effective_at,
    ))
    entries.extend(_dimension_pair(
        transaction_id, owner_id=str(user_id), asset=asset,
        customer_code="customer_contract_total_fees", system_code="total_fees_control",
        amount=fees_delta, created_at=effective_at,
    ))
    return await _commit_shadow(
        session,
        event_type=event_type,
        idempotency_key=f"contract-ledger:{legacy_entry_id}",
        entries=entries,
        effective_at=effective_at,
        source_order_id=source_order_id,
        source_trade_id=source_trade_id,
        source_event_id=source_event_id,
        source_batch_id=source_batch_id,
        source_ledger_entry_id=legacy_entry_id,
        metadata={"legacy_contract_ledger_entry_id": legacy_entry_id, **(evidence_metadata or {})},
    )


async def shadow_insurance_event(
    session: AsyncSession,
    *,
    event_id: str,
    asset: str,
    event_type: str,
    amount: Decimal,
    residual_bad_debt: Decimal,
    effective_at: datetime,
    related_liquidation_event_id: str | None,
    source_batch_id: str | None = None,
    evidence_metadata: dict | None = None,
) -> AccountingTransaction:
    transaction_id = _id("at")
    entries: list[AccountingEntry | None] = []
    if Decimal(amount) != ZERO:
        entries.extend(_dimension_pair(
            transaction_id, domain="insurance", customer_owner_type="system",
            owner_id="insurance_fund", asset=asset,
            customer_code="system_insurance_fund", system_code="insurance_fund_clearing",
            amount=Decimal(amount), created_at=effective_at,
        ))
    if event_type == "bad_debt_uncovered" and Decimal(residual_bad_debt) > ZERO:
        entries.extend(_dimension_pair(
            transaction_id, domain="liquidation", customer_owner_type="system",
            owner_id="bad_debt", asset=asset,
            customer_code="liquidation_bad_debt", system_code="bad_debt_clearing",
            amount=Decimal(residual_bad_debt), created_at=effective_at,
        ))
    return await _commit_shadow(
        session,
        event_type=f"insurance_{event_type}",
        idempotency_key=f"insurance-event:{event_id}",
        entries=entries,
        effective_at=effective_at,
        source_event_id=event_id,
        source_batch_id=source_batch_id or related_liquidation_event_id,
        metadata={
            "insurance_event_id": event_id,
            "related_liquidation_event_id": related_liquidation_event_id,
            **(evidence_metadata or {}),
        },
    )


async def shadow_voided_ledger_evidence(
    session: AsyncSession,
    *,
    source_ledger_entry_id: str,
    original_amount: Decimal,
    effective_at: datetime,
    source_batch_id: str,
    metadata: dict,
) -> AccountingTransaction:
    """Record an immutable audit marker for a legacy no-op financial event."""
    marker = abs(Decimal(original_amount)) or Decimal("0.000000000000000001")
    transaction_id = _id("at")
    entries = _dimension_pair(
        transaction_id,
        domain="audit",
        customer_owner_type="system",
        owner_id="data_quality",
        asset="AUDIT",
        customer_code="voided_financial_event",
        system_code="voided_financial_event_offset",
        amount=marker,
        created_at=effective_at,
    )
    return await _commit_shadow(
        session,
        event_type="voided_legacy_noop",
        idempotency_key=f"voided-ledger:{source_ledger_entry_id}",
        entries=entries,
        effective_at=effective_at,
        source_batch_id=source_batch_id,
        source_ledger_entry_id=source_ledger_entry_id,
        metadata={
            **metadata,
            "voided": True,
            "business_effect": "none",
            "original_amount": str(original_amount),
        },
    )


async def create_shadow_reconciliation_adjustment(
    session: AsyncSession,
    *,
    reconciliation_run_id: str,
    idempotency_key: str,
    reason: str,
    actor_username: str,
) -> tuple[AccountingTransaction, bool]:
    """Explicitly align the shadow snapshot without mutating business funds.

    This is a manual recovery primitive for a proved historical gap. It writes
    balanced customer/control entries linked to the failed reconciliation run;
    it never updates the original transaction or the business balance tables.
    """
    key = idempotency_key.strip()
    normalized_reason = reason.strip()
    normalized_actor = actor_username.strip()
    if len(key) < 8 or len(key) > 191:
        raise ValueError("idempotency_key must be 8-191 characters")
    if len(normalized_reason) < 12 or len(normalized_reason) > 500:
        raise ValueError("reason must be 12-500 characters")
    if len(normalized_actor) < 3 or len(normalized_actor) > 64:
        raise ValueError("actor_username must be 3-64 characters")
    existing = await session.scalar(
        select(AccountingTransaction).where(AccountingTransaction.idempotency_key == key)
    )
    if existing is not None:
        return existing, False
    run = await session.scalar(
        select(ReconciliationRun).where(ReconciliationRun.run_id == reconciliation_run_id)
    )
    if run is None or run.scope != "full" or run.blocking_count <= 0:
        raise ValueError("a persisted failed full reconciliation run is required")

    from app.services.accounting_proof_service import build_accounting_proof_snapshots

    business_snapshot, accounting_snapshot, _ = await build_accounting_proof_snapshots(session)
    accounting_by_key = {
        (row["domain"], row["owner_id"], row["asset"]): row
        for row in accounting_snapshot["rows"]
    }
    dimension_codes = {
        "spot": {
            "available": ("customer_spot_available", "spot_rounding_difference"),
            "frozen": ("customer_spot_frozen", "spot_rounding_difference"),
        },
        "contract": {
            "wallet": ("customer_contract_wallet", "contract_rounding_difference"),
            "available": ("customer_contract_available", "contract_rounding_difference"),
            "used_margin": ("customer_margin_used", "contract_rounding_difference"),
            "unrealized_pnl": ("customer_contract_unrealized_pnl", "unrecorded_mark_to_market_recovery"),
            "realized_pnl": ("customer_contract_realized_pnl", "contract_rounding_difference"),
            "total_fees": ("customer_contract_total_fees", "contract_rounding_difference"),
        },
        "insurance": {
            "balance": ("system_insurance_fund", "insurance_rounding_difference"),
        },
    }
    transaction_id = _id("at")
    created_at = datetime.now(tz=UTC)
    entries: list[AccountingEntry | None] = []
    adjustments: list[dict] = []
    for business_row in business_snapshot["rows"]:
        domain = business_row["domain"]
        owner_id = business_row["owner_id"]
        asset = business_row["asset"]
        accounting_row = accounting_by_key[(domain, owner_id, asset)]
        for field, (customer_code, system_code) in dimension_codes[domain].items():
            amount = Decimal(business_row[field]) - Decimal(accounting_row[field])
            if amount == ZERO:
                continue
            if domain == "contract" and field == "available" and abs(amount) > Decimal("0.00001"):
                # A material available-margin gap accompanying an unrealized
                # PnL gap is historical unledgered mark-to-market evidence,
                # not a SQLite rounding residual.
                system_code = "unrecorded_mark_to_market_recovery"
            owner_type = "system" if domain == "insurance" else "customer"
            entries.extend(_dimension_pair(
                transaction_id,
                domain=domain,
                customer_owner_type=owner_type,
                owner_id=owner_id,
                asset=asset,
                customer_code=customer_code,
                system_code=system_code,
                amount=amount,
                created_at=created_at,
            ))
            adjustments.append({
                "domain": domain,
                "owner_id": owner_id,
                "asset": asset,
                "field": field,
                "amount": str(amount),
                "system_account_code": system_code,
            })
    if not adjustments:
        raise ValueError("business and accounting snapshots already match")
    transaction = await _commit_shadow(
        session,
        event_type="shadow_reconciliation_adjustment",
        idempotency_key=key,
        entries=entries,
        effective_at=created_at,
        source_batch_id=reconciliation_run_id,
        metadata={
            "classification": "explicit_shadow_recovery",
            "reconciliation_run_id": reconciliation_run_id,
            "reason": normalized_reason,
            "actor_username": normalized_actor,
            "business_balance_mutated": False,
            "automatic_repair": False,
            "adjustment_count": len(adjustments),
            "adjustments": adjustments,
        },
        schema_version="3",
    )
    return transaction, True


async def ensure_shadow_opening_balances(session: AsyncSession) -> dict:
    """Create an explicit cutover anchor without pretending to reconstruct pruned history.

    The opening amount equals the current business snapshot minus every shadow
    movement already recorded. Re-running is idempotent, and future movements
    can therefore rebuild the live snapshot from accounting quantities.
    """
    existing_rows = await session.execute(
        select(
            AccountingEntry.account_domain,
            AccountingEntry.owner_id,
            AccountingEntry.asset,
            AccountingEntry.account_code,
            func.sum(AccountingEntry.quantity),
        ).group_by(
            AccountingEntry.account_domain,
            AccountingEntry.owner_id,
            AccountingEntry.asset,
            AccountingEntry.account_code,
        )
    )
    totals = {
        (domain, owner_id, asset, code): Decimal(total or ZERO)
        for domain, owner_id, asset, code, total in existing_rows.all()
    }
    earliest = await session.scalar(select(func.min(AccountingTransaction.effective_at)))
    if earliest is None:
        opening_at = datetime.now(tz=UTC)
    else:
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=UTC)
        opening_at = earliest - timedelta(microseconds=1)
    watermark = {
        "spot_ledger_max_id": int(await session.scalar(select(func.coalesce(func.max(LedgerEntry.id), 0))) or 0),
        "contract_ledger_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractLedgerEntry.id), 0))) or 0),
        "insurance_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractInsuranceEvent.id), 0))) or 0),
        "funding_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractFundingEvent.id), 0))) or 0),
        "liquidation_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractLiquidationEvent.id), 0))) or 0),
        "adl_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractAdlEvent.id), 0))) or 0),
    }
    created = 0
    batch_id = "shadow-opening-v2"

    # A fresh Paper durable database may already have every seeded balance
    # represented by deposit/reset shadow facts, so all calculated opening
    # deltas are zero.  Keep an explicit zero-value opening marker anyway:
    # reconciliation must be able to prove that the opening boundary was
    # evaluated, without inventing or changing any funds.
    opening_marker = await session.scalar(
        select(AccountingTransaction).where(
            AccountingTransaction.idempotency_key == "shadow-opening:v2:marker"
        )
    )
    if opening_marker is None:
        marker_asset = "USDT"
        first_balance = await session.scalar(select(Balance.asset).order_by(Balance.id.asc()))
        if first_balance:
            marker_asset = str(first_balance)
        marker_transaction_id = _id("at")
        marker_entries = [
            _line(
                marker_transaction_id,
                domain="system",
                owner_type="system",
                owner_id="exchange",
                asset=marker_asset,
                account_code="system_shadow_opening_marker",
                amount=Decimal("1"),
                debit_when_positive=True,
                created_at=opening_at,
            ),
            _line(
                marker_transaction_id,
                domain="system",
                owner_type="system",
                owner_id="opening",
                asset=marker_asset,
                account_code="system_shadow_opening_marker_offset",
                amount=Decimal("-1"),
                debit_when_positive=True,
                created_at=opening_at,
            ),
        ]
        await _commit_shadow(
            session,
            event_type="shadow_opening_balance",
            idempotency_key="shadow-opening:v2:marker",
            entries=marker_entries,
            effective_at=opening_at,
            source_batch_id=batch_id,
            metadata={**watermark, "marker_only": True, "cutover_only": True},
        )
        created += 1

    balances = (await session.execute(select(Balance).order_by(Balance.id))).scalars()
    for balance in balances:
        owner_id = str(balance.user_id)
        available = Decimal(balance.available) - totals.get(("spot", owner_id, balance.asset, "customer_spot_available"), ZERO)
        frozen = Decimal(balance.frozen) - totals.get(("spot", owner_id, balance.asset, "customer_spot_frozen"), ZERO)
        transaction_id = _id("at")
        entries = [
            _line(transaction_id, domain="spot", owner_type="customer", owner_id=owner_id,
                  asset=balance.asset, account_code="customer_spot_available", amount=available,
                  debit_when_positive=True, created_at=opening_at),
            _line(transaction_id, domain="spot", owner_type="customer", owner_id=owner_id,
                  asset=balance.asset, account_code="customer_spot_frozen", amount=frozen,
                  debit_when_positive=True, created_at=opening_at),
            _line(transaction_id, domain="spot", owner_type="system", owner_id="exchange",
                  asset=balance.asset, account_code="system_spot_opening", amount=available + frozen,
                  debit_when_positive=False, created_at=opening_at),
        ]
        if any(entry is not None for entry in entries):
            await _commit_shadow(
                session, event_type="shadow_opening_balance",
                idempotency_key=f"shadow-opening:v2:spot:{balance.user_id}:{balance.asset}",
                entries=entries, effective_at=opening_at, source_batch_id=batch_id,
                metadata={**watermark, "domain": "spot", "cutover_only": True},
            )
            created += 1

    fields = (
        ("wallet", "wallet_balance", "customer_contract_wallet"),
        ("available", "available_margin", "customer_contract_available"),
        ("used", "used_margin", "customer_margin_used"),
        ("unrealized", "unrealized_pnl", "customer_contract_unrealized_pnl"),
        ("realized", "realized_pnl", "customer_contract_realized_pnl"),
        ("fees", "total_fees", "customer_contract_total_fees"),
    )
    accounts = (await session.execute(select(ContractAccount).order_by(ContractAccount.id))).scalars()
    for account in accounts:
        owner_id = str(account.user_id)
        transaction_id = _id("at")
        entries: list[AccountingEntry | None] = []
        for label, attr, code in fields:
            base = Decimal(getattr(account, attr)) - totals.get(("contract", owner_id, account.margin_asset, code), ZERO)
            entries.extend(_dimension_pair(
                transaction_id, owner_id=owner_id, asset=account.margin_asset,
                customer_code=code, system_code=f"contract_{label}_opening",
                amount=base, created_at=opening_at,
            ))
        if any(entry is not None for entry in entries):
            await _commit_shadow(
                session, event_type="shadow_opening_balance",
                idempotency_key=f"shadow-opening:v2:contract:{account.user_id}:{account.margin_asset}",
                entries=entries, effective_at=opening_at, source_batch_id=batch_id,
                metadata={**watermark, "domain": "contract", "cutover_only": True},
            )
            created += 1

    funds = (await session.execute(select(ContractInsuranceFund).order_by(ContractInsuranceFund.id))).scalars()
    for fund in funds:
        base = Decimal(fund.balance) - totals.get(("insurance", "insurance_fund", fund.margin_asset, "system_insurance_fund"), ZERO)
        transaction_id = _id("at")
        entries = _dimension_pair(
            transaction_id, domain="insurance", customer_owner_type="system",
            owner_id="insurance_fund", asset=fund.margin_asset,
            customer_code="system_insurance_fund", system_code="insurance_fund_opening",
            amount=base, created_at=opening_at,
        )
        if any(entry is not None for entry in entries):
            await _commit_shadow(
                session, event_type="shadow_opening_balance",
                idempotency_key=f"shadow-opening:v2:insurance:{fund.margin_asset}",
                entries=entries, effective_at=opening_at, source_batch_id=batch_id,
                metadata={**watermark, "domain": "insurance", "cutover_only": True},
            )
            created += 1
    return {"batch_id": batch_id, "created": created, "watermark": watermark}


async def backfill_missing_shadow_evidence(
    session: AsyncSession,
    *,
    reconciliation_run_id: str,
    reason: str,
) -> dict:
    """Repair missing shadow evidence without changing any business snapshot.

    This is deliberately not called by reconciliation or application startup.
    The caller must name a persisted failed reconciliation run and an explicit
    reason; every created transaction records both as evidence.
    """
    reason = reason.strip()
    if not reason:
        raise ValueError("repair reason is required")
    run = await session.scalar(
        select(ReconciliationRun).where(ReconciliationRun.run_id == reconciliation_run_id)
    )
    if run is None or run.blocking_count <= 0:
        raise ValueError("a persisted reconciliation run with blocking differences is required")
    check_counts = {
        item.get("code"): int(item.get("difference_count") or 0)
        for item in (run.summary_json or {}).get("checks", [])
    }
    repairable = {
        "accounting_spot_post_cutover_missing",
        "accounting_contract_post_cutover_missing",
        "accounting_insurance_post_cutover_missing",
        "accounting_spot_source_delta_mismatch",
        "accounting_contract_source_delta_mismatch",
    }
    if not any(check_counts.get(code, 0) > 0 for code in repairable):
        raise ValueError("reconciliation run has no repairable missing shadow evidence")

    metadata = {
        "late_shadow_evidence": True,
        "reconciliation_run_id": reconciliation_run_id,
        "repair_reason": reason,
    }
    created = {"spot": 0, "contract": 0, "insurance": 0, "reversals": 0}
    spot_cutoff = int((await session.execute(text("""
        SELECT coalesce(max(CAST(json_extract(metadata_json,'$.spot_ledger_max_id') AS INTEGER)),0)
        FROM accounting_transactions WHERE event_type='shadow_opening_balance'
    """))).scalar_one() or 0)
    spot_rows = (await session.execute(
        select(LedgerEntry)
        .outerjoin(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == LedgerEntry.entry_id)
        .where(LedgerEntry.id > spot_cutoff, AccountingTransaction.transaction_id.is_(None))
        .order_by(LedgerEntry.id)
    )).scalars().all()
    for row in spot_rows:
        available_delta = Decimal(row.available_after) - Decimal(row.available_before)
        frozen_delta = Decimal(row.frozen_after) - Decimal(row.frozen_before)
        if available_delta == ZERO and frozen_delta == ZERO:
            await shadow_voided_ledger_evidence(
                session,
                source_ledger_entry_id=row.entry_id,
                original_amount=Decimal(row.amount),
                effective_at=row.created_at,
                source_batch_id=reconciliation_run_id,
                metadata={**metadata, "legacy_change_type": row.change_type, "domain": "spot"},
            )
        else:
            await shadow_spot_ledger_entry(
                session,
                legacy_entry_id=row.entry_id,
                user_id=row.user_id,
                asset=row.asset,
                event_type=row.change_type,
                available_delta=available_delta,
                frozen_delta=frozen_delta,
                effective_at=row.created_at,
                source_order_id=row.related_order_id,
                source_trade_id=row.related_trade_id,
                source_batch_id=reconciliation_run_id,
                evidence_metadata=metadata,
            )
        created["spot"] += 1

    contract_cutoff = int((await session.execute(text("""
        SELECT coalesce(max(CAST(json_extract(metadata_json,'$.contract_ledger_max_id') AS INTEGER)),0)
        FROM accounting_transactions WHERE event_type='shadow_opening_balance'
    """))).scalar_one() or 0)
    contract_rows = (await session.execute(
        select(ContractLedgerEntry)
        .outerjoin(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == ContractLedgerEntry.entry_id)
        .where(ContractLedgerEntry.id > contract_cutoff, AccountingTransaction.transaction_id.is_(None))
        .order_by(ContractLedgerEntry.id)
    )).scalars().all()
    for row in contract_rows:
        before = {
            "wallet": Decimal(row.wallet_before), "available": Decimal(row.available_before),
            "used_margin": Decimal(row.used_margin_before), "unrealized_pnl": Decimal(row.unrealized_pnl_before),
            "realized_pnl": Decimal(row.realized_pnl_before), "total_fees": Decimal(row.total_fees_before),
        }
        after = {
            "wallet": Decimal(row.wallet_after), "available": Decimal(row.available_after),
            "used_margin": Decimal(row.used_margin_after), "unrealized_pnl": Decimal(row.unrealized_pnl_after),
            "realized_pnl": Decimal(row.realized_pnl_after), "total_fees": Decimal(row.total_fees_after),
        }
        if all(before[key] == after[key] for key in before):
            await shadow_voided_ledger_evidence(
                session,
                source_ledger_entry_id=row.entry_id,
                original_amount=Decimal(row.amount),
                effective_at=row.created_at,
                source_batch_id=reconciliation_run_id,
                metadata={**metadata, "legacy_change_type": row.change_type, "domain": "contract"},
            )
        else:
            await shadow_contract_ledger_entry(
                session,
                legacy_entry_id=row.entry_id,
                user_id=row.user_id,
                asset=row.margin_asset,
                event_type=row.change_type,
                before=before,
                after=after,
                effective_at=row.created_at,
                source_order_id=row.related_order_id,
                source_trade_id=row.related_trade_id,
                source_event_id=row.related_event_id,
                source_batch_id=reconciliation_run_id,
                evidence_metadata=metadata,
            )
        created["contract"] += 1

    insurance_cutoff = int((await session.execute(text("""
        SELECT coalesce(max(CAST(json_extract(metadata_json,'$.insurance_event_max_id') AS INTEGER)),0)
        FROM accounting_transactions WHERE event_type='shadow_opening_balance'
    """))).scalar_one() or 0)
    insurance_rows = (await session.execute(
        select(ContractInsuranceEvent)
        .outerjoin(AccountingTransaction, AccountingTransaction.source_event_id == ContractInsuranceEvent.event_id)
        .where(ContractInsuranceEvent.id > insurance_cutoff, AccountingTransaction.transaction_id.is_(None))
        .order_by(ContractInsuranceEvent.id)
    )).scalars().all()
    for row in insurance_rows:
        await shadow_insurance_event(
            session,
            event_id=row.event_id,
            asset=row.margin_asset,
            event_type=row.event_type,
            amount=Decimal(row.amount),
            residual_bad_debt=Decimal(row.residual_bad_debt),
            effective_at=row.created_at,
            related_liquidation_event_id=row.related_liquidation_event_id,
            source_batch_id=reconciliation_run_id,
            evidence_metadata=metadata,
        )
        created["insurance"] += 1

    linked_spot = (await session.execute(
        select(LedgerEntry, AccountingTransaction)
        .join(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == LedgerEntry.entry_id)
        .where(LedgerEntry.id > spot_cutoff, AccountingTransaction.event_type != "voided_legacy_noop")
        .order_by(LedgerEntry.id)
    )).all()
    for row, transaction in linked_spot:
        reversal_exists = await session.scalar(
            select(AccountingTransaction.transaction_id).where(
                AccountingTransaction.reversal_of_transaction_id == transaction.transaction_id
            )
        )
        if reversal_exists is not None:
            continue
        available_delta = Decimal(row.available_after) - Decimal(row.available_before)
        frozen_delta = Decimal(row.frozen_after) - Decimal(row.frozen_before)
        if available_delta != ZERO or frozen_delta != ZERO:
            continue
        entries = (await session.execute(
            select(AccountingEntry).where(AccountingEntry.transaction_id == transaction.transaction_id)
        )).scalars().all()
        customer_delta = sum(
            (abs(Decimal(entry.quantity)) for entry in entries if entry.owner_type == "customer"), ZERO
        )
        if customer_delta <= Decimal("0.00000001"):
            continue
        await reverse_transaction(
            session,
            transaction.transaction_id,
            reason=reason,
            source_batch_id=reconciliation_run_id,
            metadata={
                **metadata,
                "classification": "noop_shadow_mismatch",
                "source_ledger_entry_id": row.entry_id,
            },
        )
        created["reversals"] += 1
    linked_contract = (await session.execute(
        select(ContractLedgerEntry, AccountingTransaction)
        .join(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == ContractLedgerEntry.entry_id)
        .where(ContractLedgerEntry.id > contract_cutoff, AccountingTransaction.event_type != "voided_legacy_noop")
        .order_by(ContractLedgerEntry.id)
    )).all()
    for row, transaction in linked_contract:
        reversal_exists = await session.scalar(
            select(AccountingTransaction.transaction_id).where(
                AccountingTransaction.reversal_of_transaction_id == transaction.transaction_id
            )
        )
        if reversal_exists is not None:
            continue
        deltas = (
            Decimal(row.wallet_after) - Decimal(row.wallet_before),
            Decimal(row.available_after) - Decimal(row.available_before),
            Decimal(row.used_margin_after) - Decimal(row.used_margin_before),
            Decimal(row.unrealized_pnl_after) - Decimal(row.unrealized_pnl_before),
            Decimal(row.realized_pnl_after) - Decimal(row.realized_pnl_before),
            Decimal(row.total_fees_after) - Decimal(row.total_fees_before),
        )
        if any(delta != ZERO for delta in deltas):
            continue
        entries = (await session.execute(
            select(AccountingEntry).where(AccountingEntry.transaction_id == transaction.transaction_id)
        )).scalars().all()
        customer_delta = sum(
            (abs(Decimal(entry.quantity)) for entry in entries if entry.owner_type == "customer"), ZERO
        )
        if customer_delta <= Decimal("0.00000001"):
            continue
        await reverse_transaction(
            session,
            transaction.transaction_id,
            reason=reason,
            source_batch_id=reconciliation_run_id,
            metadata={
                **metadata,
                "classification": "noop_shadow_mismatch",
                "source_ledger_entry_id": row.entry_id,
            },
        )
        created["reversals"] += 1
    if sum(created.values()) == 0:
        raise ValueError("no safe evidence-only repair was applicable; manual adjustment or reversal analysis required")
    await session.commit()
    return {
        "reconciliation_run_id": reconciliation_run_id,
        "reason": reason,
        "created": created,
        "business_snapshots_changed": False,
        "automatic_repair": False,
    }


async def reverse_transaction(
    session: AsyncSession,
    transaction_id: str,
    *,
    reason: str,
    effective_at: datetime | None = None,
    source_batch_id: str | None = None,
    metadata: dict | None = None,
) -> AccountingTransaction:
    original = await session.scalar(
        select(AccountingTransaction).where(AccountingTransaction.transaction_id == transaction_id)
    )
    if original is None or original.status != "committed":
        raise ValueError("committed accounting transaction not found")
    original_entries = list(
        (await session.execute(select(AccountingEntry).where(AccountingEntry.transaction_id == transaction_id))).scalars()
    )
    now = effective_at or datetime.now(tz=UTC)
    reversal_id = _id("at")
    reversed_entries = [
        AccountingEntry(
            entry_id=_id("ae"), transaction_id=reversal_id, account_domain=item.account_domain,
            owner_type=item.owner_type, owner_id=item.owner_id, asset=item.asset,
            account_code=item.account_code, debit=item.credit, credit=item.debit,
            quantity=-Decimal(item.quantity), created_at=now,
        )
        for item in original_entries
    ]
    return await _commit_shadow(
        session,
        event_type="reversal",
        idempotency_key=f"reversal:{transaction_id}",
        entries=reversed_entries,
        effective_at=now,
        source_order_id=original.source_order_id,
        source_trade_id=original.source_trade_id,
        source_event_id=original.source_event_id,
        source_batch_id=source_batch_id or original.source_batch_id,
        reversal_of_transaction_id=transaction_id,
        metadata={"reason": reason, **(metadata or {})},
    )
