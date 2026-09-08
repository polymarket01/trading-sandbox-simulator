from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DomainEventLog(Base):
    """Append-only domain event log (event sourcing hot path).

    Every fast-path place/amend/cancel (spot + contract) is durably appended
    here as one JSON row before the API acknowledges. Business tables are a
    background materialized view; restart recovers from this log first.
    """

    __tablename__ = "domain_event_log"
    __table_args__ = (
        Index("idx_domain_event_log_pending", "state", "id"),
        Index("idx_domain_event_log_symbol_time", "symbol", "created_at"),
        Index("idx_domain_event_log_kind_time", "kind", "created_at"),
        Index("idx_domain_event_log_exchange_sequence", "exchange_sequence"),
        Index("idx_domain_event_log_command_id", "command_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    command_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    exchange_sequence: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[int | None] = mapped_column(BigInteger)
    command_type: Mapped[str | None] = mapped_column(String(64))
    logical_timestamp: Mapped[int | None] = mapped_column(BigInteger)
    config_version: Mapped[str | None] = mapped_column(String(128))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    durability_mode: Mapped[str | None] = mapped_column(String(32))
    sink_class: Mapped[str] = mapped_column(String(16), nullable=False, default="critical")


class DomainEventWatermark(Base):
    """Single-row materialization watermark (id is always 1)."""

    __tablename__ = "domain_event_watermark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    materialized_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
