from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FinancialOutboxEvent(Base):
    __tablename__ = "financial_outbox_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_financial_outbox_idempotency"),
        UniqueConstraint("source_ledger_entry_id", name="uq_financial_outbox_source_ledger"),
        CheckConstraint("status IN ('pending','processing','delivered','dead')", name="ck_financial_outbox_status"),
        CheckConstraint("attempts >= 0", name="ck_financial_outbox_attempts"),
        Index("idx_financial_outbox_delivery", "status", "available_at", "id"),
        Index("idx_financial_outbox_domain_time", "account_domain", "created_at"),
        Index("idx_financial_outbox_aggregate", "aggregate_type", "aggregate_id"),
        Index("idx_financial_outbox_source_ledger", "source_ledger_entry_id"),
        Index("idx_financial_outbox_source_event", "source_event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_domain: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    accounting_transaction_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounting_transactions.transaction_id")
    )
    source_ledger_entry_id: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[int | None] = mapped_column(Integer)
    asset: Mapped[str | None] = mapped_column(String(16))
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1000))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class OutboxConsumerReceipt(Base):
    __tablename__ = "outbox_consumer_receipts"
    __table_args__ = (
        UniqueConstraint("consumer_name", "event_id", name="uq_outbox_receipt_consumer_event"),
        Index("idx_outbox_receipt_event", "event_id"),
        Index("idx_outbox_receipt_processed", "processed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    receipt_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    consumer_name: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("financial_outbox_events.event_id"), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxCheckpoint(Base):
    __tablename__ = "outbox_checkpoints"

    checkpoint_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    watermark_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class FinancialOutboxReplayRequest(Base):
    __tablename__ = "financial_outbox_replay_requests"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_outbox_replay_idempotency"),
        UniqueConstraint("event_id", "attempts_before", name="uq_outbox_replay_event_attempt"),
        CheckConstraint("attempts_before > 0", name="ck_outbox_replay_attempts_positive"),
        CheckConstraint("before_status = 'dead' AND after_status = 'pending'", name="ck_outbox_replay_transition"),
        Index("idx_outbox_replay_event_time", "event_id", "requested_at"),
        Index("idx_outbox_replay_actor_time", "actor_user_id", "requested_at"),
        Index("idx_outbox_replay_reconciliation", "reconciliation_run_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    replay_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    event_id: Mapped[str] = mapped_column(
        ForeignKey("financial_outbox_events.event_id"), nullable=False
    )
    attempts_before: Mapped[int] = mapped_column(Integer, nullable=False)
    before_status: Mapped[str] = mapped_column(String(16), nullable=False)
    after_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reconciliation_run_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_runs.run_id"), nullable=False
    )
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    prior_last_error: Mapped[str | None] = mapped_column(String(1000))
    event_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
