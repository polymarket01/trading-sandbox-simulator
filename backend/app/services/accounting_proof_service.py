from __future__ import annotations

from datetime import UTC, datetime
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import AccountingEntry
from app.models.accounting_proof import AccountingProofCheckpoint
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_insurance_fund import ContractInsuranceFund
from app.models.reconciliation_run import ReconciliationRun


PROOF_SCHEMA_VERSION = "1"
DECIMAL_QUANTUM = Decimal("0.00000001")
SPOT_CODES = ("customer_spot_available", "customer_spot_frozen")
CONTRACT_CODES = (
    "customer_contract_wallet",
    "customer_contract_available",
    "customer_margin_used",
    "customer_contract_unrealized_pnl",
    "customer_contract_realized_pnl",
    "customer_contract_total_fees",
)


def _decimal_text(value: Decimal | int | str | None) -> str:
    with localcontext() as context:
        context.prec = 80
        number = Decimal(value or 0).quantize(DECIMAL_QUANTUM, rounding=ROUND_HALF_EVEN)
    if number == 0:
        number = Decimal(0)
    return format(number, ".18f")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


async def build_accounting_proof_snapshots(session: AsyncSession) -> tuple[dict, dict, dict]:
    """Build comparable business and shadow-ledger snapshots without float math."""
    business_spot: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for row in (await session.execute(select(Balance).order_by(Balance.user_id, Balance.asset))).scalars():
        business_spot[(str(row.user_id), row.asset)] = (Decimal(row.available), Decimal(row.frozen))

    business_contract: dict[tuple[str, str], tuple[Decimal, ...]] = {}
    for row in (await session.execute(
        select(ContractAccount).order_by(ContractAccount.user_id, ContractAccount.margin_asset)
    )).scalars():
        business_contract[(str(row.user_id), row.margin_asset)] = (
            Decimal(row.wallet_balance), Decimal(row.available_margin), Decimal(row.used_margin),
            Decimal(row.unrealized_pnl), Decimal(row.realized_pnl), Decimal(row.total_fees),
        )

    business_insurance: dict[str, Decimal] = {}
    for row in (await session.execute(
        select(ContractInsuranceFund).order_by(ContractInsuranceFund.margin_asset)
    )).scalars():
        business_insurance[row.margin_asset] = Decimal(row.balance)

    accounting_totals: dict[tuple[str, str, str, str], Decimal] = defaultdict(Decimal)
    aggregate_rows = (await session.execute(
        select(
            AccountingEntry.account_domain,
            AccountingEntry.owner_id,
            AccountingEntry.asset,
            AccountingEntry.account_code,
            AccountingEntry.quantity,
        ).where(
            (AccountingEntry.account_domain.in_(("spot", "contract")))
            | (
                (AccountingEntry.account_domain == "insurance")
                & (AccountingEntry.owner_id == "insurance_fund")
                & (AccountingEntry.account_code == "system_insurance_fund")
            )
        )
    )).all()
    # SQLite SUM(NUMERIC) is a floating aggregate. Daily proof generation is
    # off the trading path, so aggregate persisted values with Decimal in
    # Python and canonicalize to the exchange's 8-decimal financial atom.
    for domain, owner_id, asset, code, quantity in aggregate_rows:
        accounting_totals[(domain, owner_id, asset, code)] += Decimal(quantity or 0)

    shadow_spot_keys = {
        (owner_id, asset)
        for domain, owner_id, asset, code in accounting_totals
        if domain == "spot" and code in SPOT_CODES
    }
    shadow_contract_keys = {
        (owner_id, asset)
        for domain, owner_id, asset, code in accounting_totals
        if domain == "contract" and code in CONTRACT_CODES
    }
    shadow_insurance_keys = {
        asset
        for domain, owner_id, asset, code in accounting_totals
        if domain == "insurance" and owner_id == "insurance_fund" and code == "system_insurance_fund"
    }

    business_rows: list[dict] = []
    accounting_rows: list[dict] = []
    for owner_id, asset in sorted(set(business_spot) | shadow_spot_keys):
        available, frozen = business_spot.get((owner_id, asset), (Decimal(0), Decimal(0)))
        common = {"domain": "spot", "owner_id": owner_id, "asset": asset}
        business_rows.append({
            **common, "available": _decimal_text(available), "frozen": _decimal_text(frozen),
        })
        accounting_rows.append({
            **common,
            "available": _decimal_text(accounting_totals.get(("spot", owner_id, asset, SPOT_CODES[0]))),
            "frozen": _decimal_text(accounting_totals.get(("spot", owner_id, asset, SPOT_CODES[1]))),
        })

    contract_labels = ("wallet", "available", "used_margin", "unrealized_pnl", "realized_pnl", "total_fees")
    for owner_id, asset in sorted(set(business_contract) | shadow_contract_keys):
        values = business_contract.get((owner_id, asset), (Decimal(0),) * len(CONTRACT_CODES))
        common = {"domain": "contract", "owner_id": owner_id, "asset": asset}
        business_rows.append({
            **common, **{label: _decimal_text(value) for label, value in zip(contract_labels, values, strict=True)},
        })
        accounting_rows.append({
            **common,
            **{
                label: _decimal_text(accounting_totals.get(("contract", owner_id, asset, code)))
                for label, code in zip(contract_labels, CONTRACT_CODES, strict=True)
            },
        })

    for asset in sorted(set(business_insurance) | shadow_insurance_keys):
        common = {"domain": "insurance", "owner_id": "insurance_fund", "asset": asset}
        business_rows.append({**common, "balance": _decimal_text(business_insurance.get(asset))})
        accounting_rows.append({
            **common,
            "balance": _decimal_text(
                accounting_totals.get(("insurance", "insurance_fund", asset, "system_insurance_fund"))
            ),
        })

    business_snapshot = {"schema_version": PROOF_SCHEMA_VERSION, "rows": business_rows}
    accounting_snapshot = {"schema_version": PROOF_SCHEMA_VERSION, "rows": accounting_rows}
    counts = {
        "spot_rows": sum(1 for row in business_rows if row["domain"] == "spot"),
        "contract_rows": sum(1 for row in business_rows if row["domain"] == "contract"),
        "insurance_rows": sum(1 for row in business_rows if row["domain"] == "insurance"),
        "total_rows": len(business_rows),
        "canonical_business_bytes": len(_canonical_json(business_snapshot).encode("utf-8")),
        "canonical_accounting_bytes": len(_canonical_json(accounting_snapshot).encode("utf-8")),
    }
    return business_snapshot, accounting_snapshot, counts


def _proof_body(
    *,
    sequence_no: int,
    checkpoint_id: str,
    previous_checkpoint_id: str | None,
    previous_proof_hash: str | None,
    reconciliation_run_id: str,
    status: str,
    difference_count: int,
    blocking_count: int,
    watermark: dict,
    counts: dict,
    business_snapshot_hash: str,
    accounting_snapshot_hash: str,
    schema_version: str,
) -> dict:
    return {
        "sequence_no": sequence_no,
        "checkpoint_id": checkpoint_id,
        "previous_checkpoint_id": previous_checkpoint_id,
        "previous_proof_hash": previous_proof_hash,
        "reconciliation_run_id": reconciliation_run_id,
        "status": status,
        "difference_count": difference_count,
        "blocking_count": blocking_count,
        "watermark": watermark,
        "counts": counts,
        "business_snapshot_hash": business_snapshot_hash,
        "accounting_snapshot_hash": accounting_snapshot_hash,
        "schema_version": schema_version,
    }


def serialize_accounting_proof(row: AccountingProofCheckpoint, *, include_snapshots: bool = False) -> dict:
    result = {
        "checkpoint_id": row.checkpoint_id,
        "idempotency_key": row.idempotency_key,
        "sequence_no": row.sequence_no,
        "previous_checkpoint_id": row.previous_checkpoint_id,
        "previous_proof_hash": row.previous_proof_hash,
        "reconciliation_run_id": row.reconciliation_run_id,
        "status": row.status,
        "difference_count": row.difference_count,
        "blocking_count": row.blocking_count,
        "watermark": row.watermark_json,
        "counts": row.counts_json,
        "business_snapshot_hash": row.business_snapshot_hash,
        "accounting_snapshot_hash": row.accounting_snapshot_hash,
        "proof_hash": row.proof_hash,
        "started_at": row.started_at.isoformat(),
        "completed_at": row.completed_at.isoformat(),
        "created_at": row.created_at.isoformat(),
        "schema_version": row.schema_version,
    }
    if include_snapshots:
        result["business_snapshot"] = row.business_snapshot_json
        result["accounting_snapshot"] = row.accounting_snapshot_json
    return result


async def create_accounting_proof_checkpoint(
    session: AsyncSession,
    *,
    reconciliation_summary: dict,
    idempotency_key: str,
) -> tuple[AccountingProofCheckpoint, bool]:
    key = idempotency_key.strip()
    if len(key) < 8 or len(key) > 191:
        raise ValueError("idempotency_key must be 8-191 characters")
    existing = await session.scalar(
        select(AccountingProofCheckpoint).where(AccountingProofCheckpoint.idempotency_key == key)
    )
    if existing is not None:
        return existing, False
    if reconciliation_summary.get("scope") != "full":
        raise ValueError("accounting proof requires a full reconciliation run")
    run_id = str(reconciliation_summary.get("run_id") or "")
    run = await session.scalar(select(ReconciliationRun).where(ReconciliationRun.run_id == run_id))
    if run is None:
        raise ValueError("persisted reconciliation run is required")

    previous = await session.scalar(
        select(AccountingProofCheckpoint).order_by(AccountingProofCheckpoint.sequence_no.desc()).limit(1)
    )
    sequence_no = (previous.sequence_no + 1) if previous else 1
    checkpoint_id = f"acp_{uuid4().hex}"
    business_snapshot, accounting_snapshot, counts = await build_accounting_proof_snapshots(session)
    business_hash = _hash(business_snapshot)
    accounting_hash = _hash(accounting_snapshot)
    snapshot_mismatch = business_hash != accounting_hash
    difference_count = int(reconciliation_summary.get("difference_count") or 0) + int(snapshot_mismatch)
    blocking_count = int(reconciliation_summary.get("blocking_count") or 0) + int(snapshot_mismatch)
    status = "passed" if blocking_count == 0 else "failed"
    watermark = dict(reconciliation_summary.get("watermark") or {})
    body = _proof_body(
        sequence_no=sequence_no,
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=previous.checkpoint_id if previous else None,
        previous_proof_hash=previous.proof_hash if previous else None,
        reconciliation_run_id=run_id,
        status=status,
        difference_count=difference_count,
        blocking_count=blocking_count,
        watermark=watermark,
        counts=counts,
        business_snapshot_hash=business_hash,
        accounting_snapshot_hash=accounting_hash,
        schema_version=PROOF_SCHEMA_VERSION,
    )
    created_at = datetime.now(tz=UTC)
    row = AccountingProofCheckpoint(
        checkpoint_id=checkpoint_id,
        idempotency_key=key,
        sequence_no=sequence_no,
        previous_checkpoint_id=previous.checkpoint_id if previous else None,
        previous_proof_hash=previous.proof_hash if previous else None,
        reconciliation_run_id=run_id,
        status=status,
        difference_count=difference_count,
        blocking_count=blocking_count,
        watermark_json=watermark,
        counts_json=counts,
        business_snapshot_json=business_snapshot,
        accounting_snapshot_json=accounting_snapshot,
        business_snapshot_hash=business_hash,
        accounting_snapshot_hash=accounting_hash,
        proof_hash=_hash(body),
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=created_at,
        schema_version=PROOF_SCHEMA_VERSION,
    )
    session.add(row)
    await session.flush()
    return row, True


async def verify_accounting_proof_chain(session: AsyncSession, *, sample_limit: int = 20) -> dict:
    rows = (await session.execute(
        select(AccountingProofCheckpoint).order_by(AccountingProofCheckpoint.sequence_no)
    )).scalars().all()
    issues: list[dict] = []
    invalid_count = 0
    previous: AccountingProofCheckpoint | None = None
    for index, row in enumerate(rows, start=1):
        row_issues: list[str] = []
        business_hash = _hash(row.business_snapshot_json)
        accounting_hash = _hash(row.accounting_snapshot_json)
        if row.sequence_no != index:
            row_issues.append("non_contiguous_sequence")
        if row.previous_checkpoint_id != (previous.checkpoint_id if previous else None):
            row_issues.append("previous_checkpoint_mismatch")
        if row.previous_proof_hash != (previous.proof_hash if previous else None):
            row_issues.append("previous_hash_mismatch")
        if row.business_snapshot_hash != business_hash:
            row_issues.append("business_snapshot_hash_mismatch")
        if row.accounting_snapshot_hash != accounting_hash:
            row_issues.append("accounting_snapshot_hash_mismatch")
        expected_status = "passed" if row.blocking_count == 0 and business_hash == accounting_hash else "failed"
        if row.status != expected_status:
            row_issues.append("status_mismatch")
        body = _proof_body(
            sequence_no=row.sequence_no,
            checkpoint_id=row.checkpoint_id,
            previous_checkpoint_id=row.previous_checkpoint_id,
            previous_proof_hash=row.previous_proof_hash,
            reconciliation_run_id=row.reconciliation_run_id,
            status=row.status,
            difference_count=row.difference_count,
            blocking_count=row.blocking_count,
            watermark=row.watermark_json,
            counts=row.counts_json,
            business_snapshot_hash=row.business_snapshot_hash,
            accounting_snapshot_hash=row.accounting_snapshot_hash,
            schema_version=row.schema_version,
        )
        if row.proof_hash != _hash(body):
            row_issues.append("proof_hash_mismatch")
        if row_issues:
            invalid_count += 1
            if len(issues) < max(1, min(sample_limit, 100)):
                issues.append({
                    "entity_id": row.checkpoint_id,
                    "sequence_no": row.sequence_no,
                    "issues": row_issues,
                })
        previous = row
    latest = serialize_accounting_proof(rows[-1]) if rows else None
    return {
        "mode": "immutable_hash_chain",
        "checkpoint_count": len(rows),
        "invalid_count": invalid_count,
        "issues": issues,
        "latest": latest,
        "hash_algorithm": "sha256",
        "canonical_decimal_scale": 8,
        "signed_or_worm": False,
    }


async def list_accounting_proofs(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    include_snapshots: bool = False,
) -> dict:
    total = int(await session.scalar(select(func.count()).select_from(AccountingProofCheckpoint)) or 0)
    rows = (await session.execute(
        select(AccountingProofCheckpoint)
        .order_by(AccountingProofCheckpoint.sequence_no.desc())
        .limit(limit)
        .offset(offset)
    )).scalars().all()
    return {
        "items": [serialize_accounting_proof(row, include_snapshots=include_snapshots) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
