from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExchangeWatermark(Base):
    __tablename__ = "exchange_watermark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingress_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    matched_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    durable_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    materialized_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    published_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    critical_materialized_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    non_critical_materialized_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="HEALTHY")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ExchangeSnapshotRecord(Base):
    __tablename__ = "exchange_snapshot_record"
    __table_args__ = (Index("idx_exchange_snapshot_seq", "snapshot_seq"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    snapshot_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
    epoch: Mapped[str] = mapped_column(String(128), nullable=False, default="legacy")
    watermarks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    committed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_version: Mapped[str] = mapped_column(String(128), nullable=False, default="runtime")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JournalTruncationCheckpoint(Base):
    __tablename__ = "journal_truncation_checkpoint"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    critical_materializer_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    outbox_receipt_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reconciliation_run_id: Mapped[str | None] = mapped_column(String(128))
    reconciliation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snapshot_hash_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backup_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    proof_hash: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
