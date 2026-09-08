from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CausalCommandJournal(Base):
    """Durable command intent and lifecycle metadata.

    The payload is immutable command input.  Lifecycle/status fields are
    intentionally separate so an unknown command can be reconciled without
    rewriting the original request.
    """

    __tablename__ = "causal_command_journal"
    __table_args__ = (
        Index("idx_causal_command_status_updated", "status", "updated_at"),
        Index("idx_causal_command_account_created", "account_id", "created_at"),
        Index("idx_causal_command_client_order", "account_id", "client_order_id"),
    )

    command_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    command_type: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    account_domain: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    market_id: Mapped[str | None] = mapped_column(String(64))
    product_type: Mapped[str] = mapped_column(String(16), nullable=False)
    epoch: Mapped[str] = mapped_column(String(128), nullable=False)
    command_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    logical_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_version: Mapped[str] = mapped_column(String(128), nullable=False)
    fee_version: Mapped[str] = mapped_column(String(128), nullable=False)
    config_version: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_instance: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int | None] = mapped_column(BigInteger)
    priority_class: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    ack_stage: Mapped[str] = mapped_column(String(32), nullable=False, default="RECEIVED")
    execution_id: Mapped[str | None] = mapped_column(String(128))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    reject_code: Mapped[str | None] = mapped_column(String(128))
    reject_stage: Mapped[str | None] = mapped_column(String(64))
    unknown_reason: Mapped[str | None] = mapped_column(String(512))
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CausalExecutionBundleRecord(Base):
    """Immutable actual execution/settlement result for one command."""

    __tablename__ = "causal_execution_bundle"
    __table_args__ = (
        Index("idx_causal_execution_command", "command_id"),
        Index("idx_causal_execution_sequence", "execution_sequence"),
        Index("idx_causal_execution_state", "state", "created_at"),
    )

    execution_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(128), nullable=False)
    epoch: Mapped[str] = mapped_column(String(128), nullable=False)
    execution_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    priority_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    book_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="DURABLE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    durable_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    materialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CausalWatermark(Base):
    """Single-row causal progress record; sequence types never overwrite one another."""

    __tablename__ = "causal_watermark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    epoch: Mapped[str] = mapped_column(String(128), nullable=False)
    ingress_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    command_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    priority_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    matched_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    execution_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    settlement_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    ledger_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    durable_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    materialized_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    published_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="STARTING")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

