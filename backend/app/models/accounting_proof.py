from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountingProofCheckpoint(Base):
    __tablename__ = "accounting_proof_checkpoints"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_accounting_proof_idempotency"),
        UniqueConstraint("sequence_no", name="uq_accounting_proof_sequence"),
        UniqueConstraint("reconciliation_run_id", name="uq_accounting_proof_reconciliation_run"),
        UniqueConstraint("proof_hash", name="uq_accounting_proof_hash"),
        CheckConstraint("sequence_no > 0", name="ck_accounting_proof_sequence_positive"),
        CheckConstraint("status IN ('passed','failed')", name="ck_accounting_proof_status"),
        CheckConstraint("difference_count >= 0 AND blocking_count >= 0", name="ck_accounting_proof_counts"),
        Index("idx_accounting_proof_completed", "completed_at"),
        Index("idx_accounting_proof_status", "status", "sequence_no"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_checkpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounting_proof_checkpoints.checkpoint_id")
    )
    previous_proof_hash: Mapped[str | None] = mapped_column(String(64))
    reconciliation_run_id: Mapped[str] = mapped_column(
        ForeignKey("reconciliation_runs.run_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    difference_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocking_count: Mapped[int] = mapped_column(Integer, nullable=False)
    watermark_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    counts_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    business_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    accounting_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    business_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accounting_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    proof_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
