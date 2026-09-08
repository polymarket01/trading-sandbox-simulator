from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounting import AccountingEntry, AccountingTransaction
from app.models.accounting_proof import AccountingProofCheckpoint
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.financial_outbox import FinancialOutboxEvent, OutboxCheckpoint
from app.models.ledger_entry import LedgerEntry
from app.models.reconciliation_run import ReconciliationRun
from app.models.robot_financial_checkpoint import RobotFinancialCheckpoint, RobotFinancialCheckpointItem
from app.models.user import User
from app.services.financial_outbox_service import OUTBOX_CHECKPOINT_NAME
from app.services.history_retention_service import user_robot_retention_condition


SCHEMA_VERSION = "3"
LEGACY_QUANTUM = Decimal("0.000000000000000001")
QUANTUM = Decimal("0.00000001")
SPOT_DIMENSIONS = ("available", "frozen")
CONTRACT_DIMENSIONS = ("wallet", "available", "used_margin", "unrealized_pnl", "realized_pnl", "total_fees")


def _decimal_text(
    value: Decimal | int | str | None,
    *,
    schema_version: str = SCHEMA_VERSION,
) -> str:
    quantum = LEGACY_QUANTUM if schema_version == "1" else QUANTUM
    with localcontext() as context:
        context.prec = 80
        number = Decimal(value or 0).quantize(quantum, rounding=ROUND_HALF_EVEN)
    if schema_version != "1" and number == 0:
        number = Decimal(0)
    return format(number, ".18f")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.isoformat()


def _spot_manifest(row: LedgerEntry, username: str, role: str, transaction_id: str | None, event_id: str | None) -> dict:
    return {
        "source_id": row.id,
        "entry_id": row.entry_id,
        "user_id": row.user_id,
        "username": username,
        "role": role,
        "asset": row.asset,
        "change_type": row.change_type,
        "before": {
            "available": _decimal_text(row.available_before),
            "frozen": _decimal_text(row.frozen_before),
        },
        "after": {
            "available": _decimal_text(row.available_after),
            "frozen": _decimal_text(row.frozen_after),
        },
        "delta": {
            "available": _decimal_text(Decimal(row.available_after) - Decimal(row.available_before)),
            "frozen": _decimal_text(Decimal(row.frozen_after) - Decimal(row.frozen_before)),
        },
        "related_order_id": row.related_order_id,
        "related_trade_id": row.related_trade_id,
        "accounting_transaction_id": transaction_id,
        "outbox_event_id": event_id,
        "created_at": _timestamp(row.created_at),
    }


def _contract_manifest(
    row: ContractLedgerEntry,
    username: str,
    role: str,
    transaction_id: str | None,
    event_id: str | None,
) -> dict:
    before_values = (
        row.wallet_before, row.available_before, row.used_margin_before,
        row.unrealized_pnl_before, row.realized_pnl_before, row.total_fees_before,
    )
    after_values = (
        row.wallet_after, row.available_after, row.used_margin_after,
        row.unrealized_pnl_after, row.realized_pnl_after, row.total_fees_after,
    )
    return {
        "source_id": row.id,
        "entry_id": row.entry_id,
        "user_id": row.user_id,
        "username": username,
        "role": role,
        "asset": row.margin_asset,
        "change_type": row.change_type,
        "before": {key: _decimal_text(value) for key, value in zip(CONTRACT_DIMENSIONS, before_values, strict=True)},
        "after": {key: _decimal_text(value) for key, value in zip(CONTRACT_DIMENSIONS, after_values, strict=True)},
        "delta": {
            key: _decimal_text(Decimal(after) - Decimal(before))
            for key, before, after in zip(CONTRACT_DIMENSIONS, before_values, after_values, strict=True)
        },
        "related_order_id": row.related_order_id,
        "related_trade_id": row.related_trade_id,
        "related_event_id": row.related_event_id,
        "accounting_transaction_id": transaction_id,
        "outbox_event_id": event_id,
        "created_at": _timestamp(row.created_at),
    }


async def _accounting_totals(
    session: AsyncSession,
    transaction_ids: list[str],
    *,
    schema_version: str = SCHEMA_VERSION,
) -> tuple[Decimal, Decimal]:
    if not transaction_ids:
        return Decimal(0), Decimal(0)
    if schema_version != "1":
        rows = (await session.execute(
            select(AccountingEntry.debit, AccountingEntry.credit)
            .where(AccountingEntry.transaction_id.in_(transaction_ids))
        )).all()
        return (
            sum((Decimal(debit or 0) for debit, _ in rows), Decimal(0)),
            sum((Decimal(credit or 0) for _, credit in rows), Decimal(0)),
        )
    debit, credit = (await session.execute(
        select(func.coalesce(func.sum(AccountingEntry.debit), 0), func.coalesce(func.sum(AccountingEntry.credit), 0))
        .where(AccountingEntry.transaction_id.in_(transaction_ids))
    )).one()
    return Decimal(debit or 0), Decimal(credit or 0)


def _summarize_manifest(
    manifest: list[dict],
    dimensions: tuple[str, ...],
    debit: Decimal,
    credit: Decimal,
    *,
    schema_version: str = SCHEMA_VERSION,
) -> tuple[dict, dict, dict]:
    opening = dict(manifest[0]["before"])
    closing = dict(manifest[-1]["after"])
    deltas = {dimension: Decimal(0) for dimension in dimensions}
    chain_gaps = 0
    previous_after: dict | None = None
    for row in manifest:
        if previous_after is not None and row["before"] != previous_after:
            chain_gaps += 1
        previous_after = row["after"]
        for dimension in dimensions:
            deltas[dimension] += Decimal(row["delta"][dimension])
    reconstructed = {
        dimension: _decimal_text(
            Decimal(opening[dimension]) + deltas[dimension], schema_version=schema_version
        )
        for dimension in dimensions
    }
    if schema_version == "3":
        closing_explained = all(
            abs(Decimal(reconstructed[dimension]) - Decimal(closing[dimension])) <= QUANTUM
            for dimension in dimensions
        )
    else:
        closing_explained = reconstructed == closing
    aggregate = {
        "delta": {
            dimension: _decimal_text(deltas[dimension], schema_version=schema_version)
            for dimension in dimensions
        },
        "reconstructed_closing": reconstructed,
        "chain_gap_count": chain_gaps,
        "debit": _decimal_text(debit, schema_version=schema_version),
        "credit": _decimal_text(credit, schema_version=schema_version),
        "balanced": debit == credit,
        "closing_explained": closing_explained,
        "evidence_complete": all(
            row["accounting_transaction_id"] is not None and row["outbox_event_id"] is not None
            for row in manifest
        ),
        "subject": {
            "username": manifest[0]["username"],
            "role": manifest[0]["role"],
            "customer_record": False,
        },
    }
    return opening, closing, aggregate


async def create_robot_financial_checkpoint(
    session: AsyncSession,
    *,
    reconciliation_summary: dict,
    accounting_proof: AccountingProofCheckpoint,
    idempotency_key: str,
) -> tuple[RobotFinancialCheckpoint, bool]:
    key = idempotency_key.strip()
    if len(key) < 8 or len(key) > 191:
        raise ValueError("idempotency_key must be 8-191 characters")
    existing = await session.scalar(
        select(RobotFinancialCheckpoint).where(RobotFinancialCheckpoint.idempotency_key == key)
    )
    if existing is not None:
        return existing, False
    if reconciliation_summary.get("scope") != "full":
        raise ValueError("robot checkpoint requires a full reconciliation run")
    run_id = str(reconciliation_summary.get("run_id") or "")
    run = await session.scalar(select(ReconciliationRun).where(ReconciliationRun.run_id == run_id))
    if run is None:
        raise ValueError("persisted reconciliation run is required")
    if accounting_proof.reconciliation_run_id != run_id:
        raise ValueError("accounting proof and robot checkpoint must share the same reconciliation run")

    previous = await session.scalar(
        select(RobotFinancialCheckpoint).order_by(RobotFinancialCheckpoint.sequence_no.desc()).limit(1)
    )
    outbox_checkpoint = await session.get(OutboxCheckpoint, OUTBOX_CHECKPOINT_NAME)
    if outbox_checkpoint is None:
        raise ValueError("financial outbox cutover checkpoint is required")
    outbox_watermark = outbox_checkpoint.watermark_json
    previous_watermark = previous.range_watermark_json if previous else {}
    spot_start = int(previous_watermark.get("spot_end_id") or outbox_watermark.get("spot_ledger_max_id") or 0)
    contract_start = int(previous_watermark.get("contract_end_id") or outbox_watermark.get("contract_ledger_max_id") or 0)
    spot_end = int(await session.scalar(select(func.coalesce(func.max(LedgerEntry.id), 0))) or 0)
    contract_end = int(await session.scalar(select(func.coalesce(func.max(ContractLedgerEntry.id), 0))) or 0)

    spot_rows = (await session.execute(
        select(
            LedgerEntry, User.username, User.role,
            AccountingTransaction.transaction_id, FinancialOutboxEvent.event_id,
        )
        .join(User, User.id == LedgerEntry.user_id)
        .outerjoin(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == LedgerEntry.entry_id)
        .outerjoin(FinancialOutboxEvent, FinancialOutboxEvent.source_ledger_entry_id == LedgerEntry.entry_id)
        .where(
            user_robot_retention_condition(User),
            LedgerEntry.id > spot_start,
            LedgerEntry.id <= spot_end,
        )
        .order_by(LedgerEntry.id)
    )).all()
    contract_rows = (await session.execute(
        select(
            ContractLedgerEntry, User.username, User.role,
            AccountingTransaction.transaction_id, FinancialOutboxEvent.event_id,
        )
        .join(User, User.id == ContractLedgerEntry.user_id)
        .outerjoin(AccountingTransaction, AccountingTransaction.source_ledger_entry_id == ContractLedgerEntry.entry_id)
        .outerjoin(FinancialOutboxEvent, FinancialOutboxEvent.source_ledger_entry_id == ContractLedgerEntry.entry_id)
        .where(
            user_robot_retention_condition(User),
            ContractLedgerEntry.id > contract_start,
            ContractLedgerEntry.id <= contract_end,
        )
        .order_by(ContractLedgerEntry.id)
    )).all()

    grouped: dict[tuple[str, int, str], list[dict]] = defaultdict(list)
    for row, username, role, transaction_id, event_id in spot_rows:
        grouped[("spot", row.user_id, row.asset)].append(
            _spot_manifest(row, username, role, transaction_id, event_id)
        )
    for row, username, role, transaction_id, event_id in contract_rows:
        grouped[("contract", row.user_id, row.margin_asset)].append(
            _contract_manifest(row, username, role, transaction_id, event_id)
        )

    checkpoint_id = f"rfc_{uuid4().hex}"
    created_at = datetime.now(tz=UTC)
    item_rows: list[RobotFinancialCheckpointItem] = []
    item_proofs: list[dict] = []
    invalid_items = 0
    for (domain, user_id, asset), manifest in sorted(grouped.items()):
        transaction_ids = sorted({row["accounting_transaction_id"] for row in manifest if row["accounting_transaction_id"]})
        outbox_ids = {row["outbox_event_id"] for row in manifest if row["outbox_event_id"]}
        debit, credit = await _accounting_totals(session, transaction_ids)
        dimensions = SPOT_DIMENSIONS if domain == "spot" else CONTRACT_DIMENSIONS
        opening, closing, aggregate = _summarize_manifest(manifest, dimensions, debit, credit)
        valid = (
            aggregate["chain_gap_count"] == 0
            and aggregate["balanced"]
            and aggregate["closing_explained"]
            and aggregate["evidence_complete"]
            and len(transaction_ids) == len(manifest)
            and len(outbox_ids) == len(manifest)
        )
        if not valid:
            invalid_items += 1
        source_manifest = {"schema_version": SCHEMA_VERSION, "rows": manifest}
        manifest_hash = _hash(source_manifest)
        item_id = f"rfci_{uuid4().hex}"
        item_rows.append(RobotFinancialCheckpointItem(
            item_id=item_id,
            checkpoint_id=checkpoint_id,
            account_domain=domain,
            user_id=user_id,
            asset=asset,
            source_start_id=int(manifest[0]["source_id"]),
            source_end_id=int(manifest[-1]["source_id"]),
            range_start_at=datetime.fromisoformat(manifest[0]["created_at"]),
            range_end_at=datetime.fromisoformat(manifest[-1]["created_at"]),
            opening_snapshot_json=opening,
            closing_snapshot_json=closing,
            aggregate_json=aggregate,
            source_count=len(manifest),
            accounting_transaction_count=len(transaction_ids),
            outbox_event_count=len(outbox_ids),
            source_manifest_json=source_manifest,
            manifest_hash=manifest_hash,
            created_at=created_at,
            schema_version=SCHEMA_VERSION,
        ))
        item_proofs.append({
            "item_id": item_id,
            "domain": domain,
            "user_id": user_id,
            "asset": asset,
            "source_start_id": manifest[0]["source_id"],
            "source_end_id": manifest[-1]["source_id"],
            "source_count": len(manifest),
            "manifest_hash": manifest_hash,
            "valid": valid,
        })

    status = "passed" if (
        int(reconciliation_summary.get("blocking_count") or 0) == 0
        and accounting_proof.status == "passed"
        and invalid_items == 0
    ) else "failed"
    watermarks = {
        "spot_start_exclusive": spot_start,
        "spot_end_id": spot_end,
        "contract_start_exclusive": contract_start,
        "contract_end_id": contract_end,
    }
    counts = {
        "item_count": len(item_rows),
        "spot_source_count": len(spot_rows),
        "contract_source_count": len(contract_rows),
        "source_count": len(spot_rows) + len(contract_rows),
        "invalid_item_count": invalid_items,
        "customer_source_count": 0,
        "raw_records_deleted": 0,
        "pruning_authorized": False,
    }
    sequence_no = previous.sequence_no + 1 if previous else 1
    body = {
        "checkpoint_id": checkpoint_id,
        "sequence_no": sequence_no,
        "previous_checkpoint_id": previous.checkpoint_id if previous else None,
        "previous_checkpoint_hash": previous.checkpoint_hash if previous else None,
        "reconciliation_run_id": run_id,
        "accounting_proof_checkpoint_id": accounting_proof.checkpoint_id,
        "status": status,
        "watermarks": watermarks,
        "counts": counts,
        "items": item_proofs,
        "schema_version": SCHEMA_VERSION,
    }
    checkpoint = RobotFinancialCheckpoint(
        checkpoint_id=checkpoint_id,
        idempotency_key=key,
        sequence_no=sequence_no,
        previous_checkpoint_id=previous.checkpoint_id if previous else None,
        previous_checkpoint_hash=previous.checkpoint_hash if previous else None,
        reconciliation_run_id=run_id,
        accounting_proof_checkpoint_id=accounting_proof.checkpoint_id,
        status=status,
        range_watermark_json=watermarks,
        counts_json=counts,
        checkpoint_hash=_hash(body),
        created_at=created_at,
        schema_version=SCHEMA_VERSION,
    )
    session.add(checkpoint)
    await session.flush()
    session.add_all(item_rows)
    await session.flush()
    return checkpoint, True


def serialize_robot_checkpoint(row: RobotFinancialCheckpoint) -> dict:
    return {
        "checkpoint_id": row.checkpoint_id,
        "idempotency_key": row.idempotency_key,
        "sequence_no": row.sequence_no,
        "previous_checkpoint_id": row.previous_checkpoint_id,
        "previous_checkpoint_hash": row.previous_checkpoint_hash,
        "reconciliation_run_id": row.reconciliation_run_id,
        "accounting_proof_checkpoint_id": row.accounting_proof_checkpoint_id,
        "status": row.status,
        "watermarks": row.range_watermark_json,
        "counts": row.counts_json,
        "checkpoint_hash": row.checkpoint_hash,
        "created_at": row.created_at.isoformat(),
        "schema_version": row.schema_version,
    }


async def verify_robot_financial_checkpoint_chain(session: AsyncSession, *, sample_limit: int = 20) -> dict:
    checkpoints = (await session.execute(
        select(RobotFinancialCheckpoint).order_by(RobotFinancialCheckpoint.sequence_no)
    )).scalars().all()
    issues: list[dict] = []
    invalid_count = 0
    previous: RobotFinancialCheckpoint | None = None
    for expected_sequence, checkpoint in enumerate(checkpoints, start=1):
        checkpoint_issues: list[str] = []
        if checkpoint.sequence_no != expected_sequence:
            checkpoint_issues.append("non_contiguous_sequence")
        if checkpoint.previous_checkpoint_id != (previous.checkpoint_id if previous else None):
            checkpoint_issues.append("previous_checkpoint_mismatch")
        if checkpoint.previous_checkpoint_hash != (previous.checkpoint_hash if previous else None):
            checkpoint_issues.append("previous_hash_mismatch")
        items = (await session.execute(
            select(RobotFinancialCheckpointItem)
            .where(RobotFinancialCheckpointItem.checkpoint_id == checkpoint.checkpoint_id)
            .order_by(
                RobotFinancialCheckpointItem.account_domain,
                RobotFinancialCheckpointItem.user_id,
                RobotFinancialCheckpointItem.asset,
            )
        )).scalars().all()
        item_proofs: list[dict] = []
        invalid_items = 0
        source_count = 0
        for item in items:
            manifest = list((item.source_manifest_json or {}).get("rows") or [])
            item_issues: list[str] = []
            business_valid = False
            if _hash(item.source_manifest_json) != item.manifest_hash:
                item_issues.append("manifest_hash_mismatch")
            if len(manifest) != item.source_count:
                item_issues.append("source_count_mismatch")
            if manifest:
                dimensions = SPOT_DIMENSIONS if item.account_domain == "spot" else CONTRACT_DIMENSIONS
                opening, closing, aggregate = _summarize_manifest(
                    manifest,
                    dimensions,
                    Decimal(item.aggregate_json.get("debit") or 0),
                    Decimal(item.aggregate_json.get("credit") or 0),
                    schema_version=item.schema_version,
                )
                for key in ("delta", "reconstructed_closing", "chain_gap_count", "balanced", "closing_explained", "evidence_complete", "subject"):
                    if aggregate[key] != item.aggregate_json.get(key):
                        item_issues.append(f"aggregate_{key}_mismatch")
                if opening != item.opening_snapshot_json or closing != item.closing_snapshot_json:
                    item_issues.append("endpoint_snapshot_mismatch")
                transaction_ids = sorted({row["accounting_transaction_id"] for row in manifest if row.get("accounting_transaction_id")})
                outbox_ids = sorted({row["outbox_event_id"] for row in manifest if row.get("outbox_event_id")})
                transaction_count = len(transaction_ids)
                outbox_count = len(outbox_ids)
                if transaction_count != item.accounting_transaction_count:
                    item_issues.append("accounting_count_mismatch")
                if outbox_count != item.outbox_event_count:
                    item_issues.append("outbox_count_mismatch")
                existing_transaction_count = int(await session.scalar(
                    select(func.count()).select_from(AccountingTransaction)
                    .where(AccountingTransaction.transaction_id.in_(transaction_ids))
                ) or 0) if transaction_ids else 0
                current_debit, current_credit = await _accounting_totals(
                    session, transaction_ids, schema_version=item.schema_version
                )
                if existing_transaction_count != transaction_count:
                    item_issues.append("accounting_source_missing")
                if (
                    _decimal_text(current_debit, schema_version=item.schema_version)
                    != item.aggregate_json.get("debit")
                    or _decimal_text(current_credit, schema_version=item.schema_version)
                    != item.aggregate_json.get("credit")
                ):
                    item_issues.append("accounting_source_total_mismatch")
                existing_outbox_count = int(await session.scalar(
                    select(func.count()).select_from(FinancialOutboxEvent)
                    .where(FinancialOutboxEvent.event_id.in_(outbox_ids))
                ) or 0) if outbox_ids else 0
                if existing_outbox_count != outbox_count:
                    item_issues.append("outbox_source_missing")
                business_valid = (
                    aggregate["chain_gap_count"] == 0
                    and aggregate["balanced"]
                    and aggregate["closing_explained"]
                    and aggregate["evidence_complete"]
                    and transaction_count == len(manifest)
                    and outbox_count == len(manifest)
                )
            if not business_valid:
                invalid_items += 1
            if item_issues:
                checkpoint_issues.extend(f"{item.item_id}:{issue}" for issue in item_issues)
            source_count += item.source_count
            item_proofs.append({
                "item_id": item.item_id,
                "domain": item.account_domain,
                "user_id": item.user_id,
                "asset": item.asset,
                "source_start_id": item.source_start_id,
                "source_end_id": item.source_end_id,
                "source_count": item.source_count,
                "manifest_hash": item.manifest_hash,
                # The checkpoint hash commits to the financial validity that
                # was evaluated at capture time. Integrity/tamper issues are
                # reported separately and must not rewrite a legitimate failed
                # checkpoint into a different hash body during verification.
                "valid": business_valid,
            })
        counts = checkpoint.counts_json
        if len(items) != int(counts.get("item_count") or 0) or source_count != int(counts.get("source_count") or 0):
            checkpoint_issues.append("batch_count_mismatch")
        if invalid_items != int(counts.get("invalid_item_count") or 0):
            checkpoint_issues.append("invalid_item_count_mismatch")
        expected_status = "passed" if invalid_items == 0 else "failed"
        if checkpoint.status == "passed" and expected_status != "passed":
            checkpoint_issues.append("status_mismatch")
        body = {
            "checkpoint_id": checkpoint.checkpoint_id,
            "sequence_no": checkpoint.sequence_no,
            "previous_checkpoint_id": checkpoint.previous_checkpoint_id,
            "previous_checkpoint_hash": checkpoint.previous_checkpoint_hash,
            "reconciliation_run_id": checkpoint.reconciliation_run_id,
            "accounting_proof_checkpoint_id": checkpoint.accounting_proof_checkpoint_id,
            "status": checkpoint.status,
            "watermarks": checkpoint.range_watermark_json,
            "counts": checkpoint.counts_json,
            "items": item_proofs,
            "schema_version": checkpoint.schema_version,
        }
        if _hash(body) != checkpoint.checkpoint_hash:
            checkpoint_issues.append("checkpoint_hash_mismatch")
        if checkpoint_issues:
            invalid_count += 1
            if len(issues) < max(1, min(sample_limit, 100)):
                issues.append({
                    "entity_id": checkpoint.checkpoint_id,
                    "sequence_no": checkpoint.sequence_no,
                    "issues": checkpoint_issues,
                })
        previous = checkpoint
    return {
        "mode": "robot_financial_checkpoint_v1",
        "checkpoint_count": len(checkpoints),
        "invalid_count": invalid_count,
        "issues": issues,
        "latest": serialize_robot_checkpoint(checkpoints[-1]) if checkpoints else None,
        "financial_pruning_authorized": False,
        "raw_records_deleted": 0,
    }


async def list_robot_financial_checkpoints(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    total = int(await session.scalar(select(func.count()).select_from(RobotFinancialCheckpoint)) or 0)
    rows = (await session.execute(
        select(RobotFinancialCheckpoint)
        .order_by(RobotFinancialCheckpoint.sequence_no.desc())
        .limit(limit)
        .offset(offset)
    )).scalars().all()
    return {
        "items": [serialize_robot_checkpoint(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "verification": await verify_robot_financial_checkpoint_chain(session),
    }
