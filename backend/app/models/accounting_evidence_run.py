from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountingEvidenceRun(Base):
    """Mutable operational state for one UTC-day evidence capture attempt.

    The linked reconciliation/proof/checkpoint rows remain immutable. This row
    exists so a crash or a failed daily capture is visible and retryable instead
    of being inferred from an in-memory task.
    """

    __tablename__ = "accounting_evidence_runs"
    __table_args__ = (
        UniqueConstraint("observation_date", name="uq_accounting_evidence_observation_date"),
        UniqueConstraint("run_key", name="uq_accounting_evidence_run_key"),
        UniqueConstraint("reconciliation_run_id", name="uq_accounting_evidence_reconciliation"),
        UniqueConstraint("accounting_proof_checkpoint_id", name="uq_accounting_evidence_proof"),
        UniqueConstraint("robot_checkpoint_id", name="uq_accounting_evidence_robot_checkpoint"),
        CheckConstraint(
            "status IN ('running','passed','failed')",
            name="ck_accounting_evidence_status",
        ),
        CheckConstraint("attempt_count > 0", name="ck_accounting_evidence_attempt_count"),
        Index("idx_accounting_evidence_status_date", "status", "observation_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_key: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reconciliation_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("reconciliation_runs.run_id")
    )
    accounting_proof_checkpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounting_proof_checkpoints.checkpoint_id")
    )
    robot_checkpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("robot_financial_checkpoints.checkpoint_id")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(96))
    last_error_message: Mapped[str | None] = mapped_column(String(500))
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

