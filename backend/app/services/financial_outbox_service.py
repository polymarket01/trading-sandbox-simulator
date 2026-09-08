from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from typing import Awaitable, Callable
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.accounting import AccountingTransaction
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_insurance_event import ContractInsuranceEvent
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.financial_outbox import (
    FinancialOutboxEvent,
    FinancialOutboxReplayRequest,
    OutboxCheckpoint,
    OutboxConsumerReceipt,
)
from app.models.ledger_entry import LedgerEntry
from app.models.reconciliation_run import ReconciliationRun
from app.models.trade import Trade
from app.models.user import User


OUTBOX_CHECKPOINT_NAME = "financial-outbox-v1"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _event_fact_hash(event: FinancialOutboxEvent) -> str:
    fact = {
        "event_id": event.event_id,
        "idempotency_key": event.idempotency_key,
        "event_type": event.event_type,
        "account_domain": event.account_domain,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "accounting_transaction_id": event.accounting_transaction_id,
        "source_ledger_entry_id": event.source_ledger_entry_id,
        "source_event_id": event.source_event_id,
        "user_id": event.user_id,
        "asset": event.asset,
        "payload": event.payload_json,
        "occurred_at": event.occurred_at.isoformat(),
        "schema_version": event.schema_version,
    }
    return sha256(_canonical_json(fact).encode("utf-8")).hexdigest()


def serialize_outbox_replay_request(row: FinancialOutboxReplayRequest) -> dict:
    return {
        "replay_id": row.replay_id,
        "idempotency_key": row.idempotency_key,
        "event_id": row.event_id,
        "attempts_before": row.attempts_before,
        "before_status": row.before_status,
        "after_status": row.after_status,
        "reconciliation_run_id": row.reconciliation_run_id,
        "actor_user_id": row.actor_user_id,
        "actor_username": row.actor_username,
        "reason": row.reason,
        "prior_last_error": row.prior_last_error,
        "event_payload_hash": row.event_payload_hash,
        "requested_at": row.requested_at.isoformat(),
        "available_at": row.available_at.isoformat(),
        "schema_version": row.schema_version,
    }


async def request_financial_outbox_replay(
    session: AsyncSession,
    *,
    event_id: str,
    expected_attempts: int,
    idempotency_key: str,
    reason: str,
    reconciliation_run_id: str,
    actor: User,
    now: datetime | None = None,
) -> tuple[FinancialOutboxReplayRequest, bool]:
    """Move one dead delivery back to pending without changing event facts."""
    key = idempotency_key.strip()
    normalized_reason = reason.strip()
    if len(key) < 8 or len(key) > 191:
        raise ValueError("idempotency_key must be 8-191 characters")
    if len(normalized_reason) < 12 or len(normalized_reason) > 500:
        raise ValueError("reason must be 12-500 characters")
    if expected_attempts <= 0:
        raise ValueError("expected_attempts must be positive")
    existing = await session.scalar(
        select(FinancialOutboxReplayRequest).where(
            FinancialOutboxReplayRequest.idempotency_key == key
        )
    )
    if existing is not None:
        if existing.event_id != event_id:
            raise ValueError("idempotency_key is already bound to another event")
        return existing, False

    reconciliation = await session.scalar(
        select(ReconciliationRun).where(ReconciliationRun.run_id == reconciliation_run_id)
    )
    if reconciliation is None:
        raise ValueError("persisted reconciliation run is required")
    if (
        reconciliation.scope != "full"
        or reconciliation.blocking_count != 0
        or reconciliation.allow_trading != "yes"
    ):
        raise ValueError("replay requires full reconciliation with zero blocking differences")
    event = await session.scalar(
        select(FinancialOutboxEvent).where(FinancialOutboxEvent.event_id == event_id)
    )
    if event is None:
        raise ValueError("financial outbox event not found")
    if event.status != "dead":
        raise ValueError("only dead financial outbox events can be replayed")
    if event.attempts != expected_attempts:
        raise ValueError("expected_attempts does not match current event state")

    requested_at = now or datetime.now(tz=UTC)
    request = FinancialOutboxReplayRequest(
        replay_id=_id("for"),
        idempotency_key=key,
        event_id=event.event_id,
        attempts_before=event.attempts,
        before_status="dead",
        after_status="pending",
        reconciliation_run_id=reconciliation.run_id,
        actor_user_id=actor.id,
        actor_username=actor.username,
        reason=normalized_reason,
        prior_last_error=event.last_error,
        event_payload_hash=_event_fact_hash(event),
        requested_at=requested_at,
        available_at=requested_at,
        schema_version="1",
    )
    updated = await session.execute(
        update(FinancialOutboxEvent)
        .where(
            FinancialOutboxEvent.event_id == event.event_id,
            FinancialOutboxEvent.status == "dead",
            FinancialOutboxEvent.attempts == expected_attempts,
        )
        .values(
            status="pending",
            available_at=requested_at,
            claimed_by=None,
            claimed_at=None,
            delivered_at=None,
        )
    )
    if updated.rowcount != 1:
        raise ValueError("financial outbox event changed before replay was committed")
    session.add(request)
    await session.flush()
    return request, True


async def list_financial_outbox_replay_requests(
    session: AsyncSession,
    *,
    event_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(FinancialOutboxReplayRequest)
    count_stmt = select(func.count()).select_from(FinancialOutboxReplayRequest)
    if event_id:
        stmt = stmt.where(FinancialOutboxReplayRequest.event_id == event_id)
        count_stmt = count_stmt.where(FinancialOutboxReplayRequest.event_id == event_id)
    bounded_limit = max(1, min(limit, 200))
    bounded_offset = max(0, offset)
    total = int(await session.scalar(count_stmt) or 0)
    rows = (await session.execute(
        stmt.order_by(FinancialOutboxReplayRequest.id.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    )).scalars().all()
    return {
        "items": [serialize_outbox_replay_request(row) for row in rows],
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


async def verify_financial_outbox_replay_requests(
    session: AsyncSession,
    *,
    sample_limit: int = 20,
) -> dict:
    rows = (await session.execute(
        select(FinancialOutboxReplayRequest, FinancialOutboxEvent, ReconciliationRun)
        .join(FinancialOutboxEvent, FinancialOutboxEvent.event_id == FinancialOutboxReplayRequest.event_id)
        .join(ReconciliationRun, ReconciliationRun.run_id == FinancialOutboxReplayRequest.reconciliation_run_id)
        .order_by(FinancialOutboxReplayRequest.id)
    )).all()
    issues: list[dict] = []
    invalid_count = 0
    for replay, event, reconciliation in rows:
        row_issues: list[str] = []
        if replay.event_payload_hash != _event_fact_hash(event):
            row_issues.append("event_fact_hash_mismatch")
        if event.attempts < replay.attempts_before:
            row_issues.append("attempt_counter_regressed")
        if replay.before_status != "dead" or replay.after_status != "pending":
            row_issues.append("invalid_replay_transition")
        if (
            reconciliation.scope != "full"
            or reconciliation.blocking_count != 0
            or reconciliation.allow_trading != "yes"
        ):
            row_issues.append("reconciliation_gate_invalid")
        if row_issues:
            invalid_count += 1
            if len(issues) < max(1, min(sample_limit, 100)):
                issues.append({
                    "entity_id": replay.replay_id,
                    "event_id": replay.event_id,
                    "issues": row_issues,
                })
    return {
        "mode": "immutable_controlled_replay_requests",
        "request_count": len(rows),
        "invalid_count": invalid_count,
        "issues": issues,
        "automatic_repair": False,
    }


async def enqueue_financial_outbox(
    session: AsyncSession,
    *,
    event_type: str,
    account_domain: str,
    aggregate_type: str,
    aggregate_id: str,
    idempotency_key: str,
    occurred_at: datetime,
    payload: dict,
    accounting_transaction_id: str | None = None,
    source_ledger_entry_id: str | None = None,
    source_event_id: str | None = None,
    user_id: int | None = None,
    asset: str | None = None,
) -> FinancialOutboxEvent:
    existing = await session.scalar(
        select(FinancialOutboxEvent).where(FinancialOutboxEvent.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    now = datetime.now(tz=UTC)
    event = FinancialOutboxEvent(
        event_id=_id("foe"),
        idempotency_key=idempotency_key,
        event_type=event_type,
        account_domain=account_domain,
        aggregate_type=aggregate_type,
        aggregate_id=str(aggregate_id),
        accounting_transaction_id=accounting_transaction_id,
        source_ledger_entry_id=source_ledger_entry_id,
        source_event_id=source_event_id,
        user_id=user_id,
        asset=asset,
        payload_json=payload,
        status="pending",
        attempts=0,
        available_at=now,
        occurred_at=occurred_at,
        created_at=now,
        schema_version="1",
    )
    session.add(event)
    await session.flush()
    return event


async def ensure_financial_outbox_checkpoint(session: AsyncSession) -> OutboxCheckpoint:
    checkpoint = await session.get(OutboxCheckpoint, OUTBOX_CHECKPOINT_NAME)
    if checkpoint is not None:
        if "trade_max_id" not in checkpoint.watermark_json:
            watermark = dict(checkpoint.watermark_json)
            watermark["trade_max_id"] = int(
                await session.scalar(select(func.coalesce(func.max(Trade.id), 0))) or 0
            )
            watermark["trade_feature_cutover_at"] = datetime.now(tz=UTC).isoformat()
            checkpoint.watermark_json = watermark
            await session.flush()
        return checkpoint
    watermark = {
        "spot_ledger_max_id": int(await session.scalar(select(func.coalesce(func.max(LedgerEntry.id), 0))) or 0),
        "contract_ledger_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractLedgerEntry.id), 0))) or 0),
        "insurance_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractInsuranceEvent.id), 0))) or 0),
        "funding_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractFundingEvent.id), 0))) or 0),
        "liquidation_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractLiquidationEvent.id), 0))) or 0),
        "adl_event_max_id": int(await session.scalar(select(func.coalesce(func.max(ContractAdlEvent.id), 0))) or 0),
        "accounting_transaction_count": int(
            await session.scalar(select(func.count()).select_from(AccountingTransaction)) or 0
        ),
        "trade_max_id": int(await session.scalar(select(func.coalesce(func.max(Trade.id), 0))) or 0),
    }
    checkpoint = OutboxCheckpoint(
        checkpoint_name=OUTBOX_CHECKPOINT_NAME,
        watermark_json=watermark,
        created_at=datetime.now(tz=UTC),
        schema_version="1",
    )
    session.add(checkpoint)
    await session.flush()
    return checkpoint


class FinancialOutboxDispatcher:
    """At-least-once dispatcher with durable consumer receipts.

    A crash after the handler side effect but before the receipt can replay the
    event. Every payload therefore carries event_id and downstream consumers
    must deduplicate by that value. Receipt uniqueness prevents duplicate
    processing after a successful durable acknowledgement.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        consumer_name: str,
        handler: Callable[[dict], Awaitable[dict | None]],
        worker_id: str,
        batch_size: int = 100,
        lease_seconds: int = 30,
        max_attempts: int = 8,
        base_retry_seconds: float = 0.5,
    ) -> None:
        self.session_factory = session_factory
        self.consumer_name = consumer_name
        self.handler = handler
        self.worker_id = worker_id
        self.batch_size = max(1, min(batch_size, 500))
        self.lease_seconds = max(1, lease_seconds)
        self.max_attempts = max(1, max_attempts)
        self.base_retry_seconds = max(0.01, base_retry_seconds)

    async def run_once(self) -> dict:
        claimed_ids = await self._claim()
        result = {"claimed": len(claimed_ids), "delivered": 0, "retried": 0, "dead": 0, "deduplicated": 0}
        for event_id in claimed_ids:
            outcome = await self._deliver(event_id)
            result[outcome] += 1
        return result

    async def _claim(self) -> list[str]:
        now = datetime.now(tz=UTC)
        stale_before = now - timedelta(seconds=self.lease_seconds)
        claimable = or_(
            and_(FinancialOutboxEvent.status == "pending", FinancialOutboxEvent.available_at <= now),
            and_(FinancialOutboxEvent.status == "processing", FinancialOutboxEvent.claimed_at < stale_before),
        )
        async with self.session_factory() as session:
            candidates = list(
                (await session.execute(
                    select(FinancialOutboxEvent.id, FinancialOutboxEvent.event_id)
                    .where(claimable)
                    .order_by(FinancialOutboxEvent.id.asc())
                    .limit(self.batch_size)
                )).all()
            )
            claimed: list[str] = []
            for row_id, event_id in candidates:
                statement = (
                    update(FinancialOutboxEvent)
                    .where(FinancialOutboxEvent.id == row_id, claimable)
                    .values(
                        status="processing",
                        claimed_by=self.worker_id,
                        claimed_at=now,
                        attempts=FinancialOutboxEvent.attempts + 1,
                    )
                )
                updated = await session.execute(statement)
                if updated.rowcount == 1:
                    claimed.append(event_id)
            await session.commit()
            return claimed

    async def _deliver(self, event_id: str) -> str:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(FinancialOutboxEvent).where(FinancialOutboxEvent.event_id == event_id)
            )
            if event is None:
                return "deduplicated"
            receipt = await session.scalar(
                select(OutboxConsumerReceipt).where(
                    OutboxConsumerReceipt.consumer_name == self.consumer_name,
                    OutboxConsumerReceipt.event_id == event_id,
                )
            )
            if receipt is not None:
                event.status = "delivered"
                event.delivered_at = receipt.processed_at
                event.claimed_by = None
                event.claimed_at = None
                event.last_error = None
                await session.commit()
                return "deduplicated"
            snapshot = self.serialize_event(event)
        try:
            handler_result = await self.handler(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with self.session_factory() as session:
                event = await session.scalar(
                    select(FinancialOutboxEvent).where(FinancialOutboxEvent.event_id == event_id)
                )
                if event is None:
                    return "retried"
                event.last_error = str(exc)[:1000]
                event.claimed_by = None
                event.claimed_at = None
                if event.attempts >= self.max_attempts:
                    event.status = "dead"
                    outcome = "dead"
                else:
                    delay = min(60.0, self.base_retry_seconds * (2 ** max(event.attempts - 1, 0)))
                    event.status = "pending"
                    event.available_at = datetime.now(tz=UTC) + timedelta(seconds=delay)
                    outcome = "retried"
                await session.commit()
                return outcome
        now = datetime.now(tz=UTC)
        async with self.session_factory() as session:
            event = await session.scalar(
                select(FinancialOutboxEvent).where(FinancialOutboxEvent.event_id == event_id)
            )
            if event is None:
                return "deduplicated"
            receipt = await session.scalar(
                select(OutboxConsumerReceipt).where(
                    OutboxConsumerReceipt.consumer_name == self.consumer_name,
                    OutboxConsumerReceipt.event_id == event_id,
                )
            )
            if receipt is None:
                session.add(OutboxConsumerReceipt(
                    receipt_id=_id("ocr"),
                    consumer_name=self.consumer_name,
                    event_id=event_id,
                    result_json=handler_result or {},
                    processed_at=now,
                ))
            event.status = "delivered"
            event.delivered_at = now
            event.claimed_by = None
            event.claimed_at = None
            event.last_error = None
            await session.commit()
        return "delivered"

    @staticmethod
    def serialize_event(event: FinancialOutboxEvent) -> dict:
        return {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "account_domain": event.account_domain,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "accounting_transaction_id": event.accounting_transaction_id,
            "source_ledger_entry_id": event.source_ledger_entry_id,
            "source_event_id": event.source_event_id,
            "user_id": event.user_id,
            "asset": event.asset,
            "payload": event.payload_json,
            "attempts": event.attempts,
            "occurred_at": event.occurred_at.isoformat(),
            "schema_version": event.schema_version,
        }


async def deliver_financial_runtime_event(runtime, event: dict) -> dict:
    runtime.financial_outbox_metrics["last_event_id"] = event["event_id"]
    runtime.financial_outbox_metrics["last_event_type"] = event["event_type"]
    runtime.financial_outbox_metrics["last_delivered_at"] = datetime.now(tz=UTC).isoformat()
    runtime.financial_outbox_metrics["delivered"] = int(runtime.financial_outbox_metrics.get("delivered") or 0) + 1
    user_id = event.get("user_id")
    if user_id is not None:
        await runtime.ws.broadcast_private(
            int(user_id),
            "financial_events",
            {
                "channel": "financial_events",
                "type": "committed",
                "event_id": event["event_id"],
                "event_type": event["event_type"],
                "account_domain": event["account_domain"],
                "aggregate_id": event["aggregate_id"],
                "accounting_transaction_id": event.get("accounting_transaction_id"),
                "source_ledger_entry_id": event.get("source_ledger_entry_id"),
                "source_event_id": event.get("source_event_id"),
                "asset": event.get("asset"),
                "occurred_at": event["occurred_at"],
            },
        )
    return {"runtime_projection": "delivered", "event_id": event["event_id"]}


async def financial_outbox_summary(session: AsyncSession) -> dict:
    rows = (await session.execute(
        select(FinancialOutboxEvent.status, func.count()).group_by(FinancialOutboxEvent.status)
    )).all()
    counts = {status: int(count) for status, count in rows}
    checkpoint = await session.get(OutboxCheckpoint, OUTBOX_CHECKPOINT_NAME)
    oldest_pending = await session.scalar(
        select(func.min(FinancialOutboxEvent.created_at)).where(
            FinancialOutboxEvent.status.in_(["pending", "processing"])
        )
    )
    retry_events = int(
        await session.scalar(
            select(func.count()).select_from(FinancialOutboxEvent).where(FinancialOutboxEvent.attempts > 1)
        )
        or 0
    )
    receipts = int(await session.scalar(select(func.count()).select_from(OutboxConsumerReceipt)) or 0)
    replay_requests = int(
        await session.scalar(select(func.count()).select_from(FinancialOutboxReplayRequest)) or 0
    )
    return {
        "mode": "transactional_database_outbox",
        "delivery_semantics": "at_least_once",
        "consumer_deduplication_key": "event_id",
        "checkpoint": None if checkpoint is None else {
            "name": checkpoint.checkpoint_name,
            "watermark": checkpoint.watermark_json,
            "created_at": checkpoint.created_at.isoformat(),
        },
        "counts": {
            "pending": counts.get("pending", 0),
            "processing": counts.get("processing", 0),
            "delivered": counts.get("delivered", 0),
            "dead": counts.get("dead", 0),
            "total": sum(counts.values()),
            "receipts": receipts,
            "retry_events": retry_events,
            "replay_requests": replay_requests,
        },
        "oldest_pending_at": None if oldest_pending is None else oldest_pending.isoformat(),
        "automatic_balance_repair": False,
    }


async def list_financial_outbox_events(
    session: AsyncSession,
    *,
    status: str | None = None,
    domain: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    stmt = select(FinancialOutboxEvent)
    count_stmt = select(func.count()).select_from(FinancialOutboxEvent)
    if status:
        stmt = stmt.where(FinancialOutboxEvent.status == status)
        count_stmt = count_stmt.where(FinancialOutboxEvent.status == status)
    if domain:
        stmt = stmt.where(FinancialOutboxEvent.account_domain == domain)
        count_stmt = count_stmt.where(FinancialOutboxEvent.account_domain == domain)
    bounded_limit = max(1, min(limit, 500))
    bounded_offset = max(0, offset)
    rows = (await session.execute(
        stmt.order_by(FinancialOutboxEvent.id.desc()).offset(bounded_offset).limit(bounded_limit)
    )).scalars().all()
    total = int(await session.scalar(count_stmt) or 0)
    items = [{
        **FinancialOutboxDispatcher.serialize_event(row),
        "status": row.status,
        "idempotency_key": row.idempotency_key,
        "attempts": row.attempts,
        "available_at": row.available_at.isoformat(),
        "claimed_by": row.claimed_by,
        "claimed_at": None if row.claimed_at is None else row.claimed_at.isoformat(),
        "delivered_at": None if row.delivered_at is None else row.delivered_at.isoformat(),
        "last_error": row.last_error,
        "created_at": row.created_at.isoformat(),
    } for row in rows]
    return {"items": items, "total": total, "limit": bounded_limit, "offset": bounded_offset}
