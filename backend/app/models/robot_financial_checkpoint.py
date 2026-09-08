from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RobotFinancialCheckpoint(Base):
    __tablename__ = "robot_financial_checkpoints"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_robot_financial_checkpoint_idempotency"),
        UniqueConstraint("sequence_no", name="uq_robot_financial_checkpoint_sequence"),
        UniqueConstraint("reconciliation_run_id", name="uq_robot_financial_checkpoint_reconciliation"),
        UniqueConstraint("accounting_proof_checkpoint_id", name="uq_robot_financial_checkpoint_proof"),
        UniqueConstraint("checkpoint_hash", name="uq_robot_financial_checkpoint_hash"),
        CheckConstraint("sequence_no > 0", name="ck_robot_financial_checkpoint_sequence"),
        CheckConstraint("status IN ('passed','failed')", name="ck_robot_financial_checkpoint_status"),
        Index("idx_robot_financial_checkpoint_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    checkpoint_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_checkpoint_id: Mapped[str | None] = mapped_column(
        ForeignKey("robot_financial_checkpoints.checkpoint_id")
    )
    previous_checkpoint_hash: Mapped[str | None] = mapped_column(String(64))
    reconciliation_run_id: Mapped[str] = mapped_column(ForeignKey("reconciliation_runs.run_id"), nullable=False)
    accounting_proof_checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("accounting_proof_checkpoints.checkpoint_id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    range_watermark_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    counts_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    checkpoint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class RobotFinancialCheckpointItem(Base):
    __tablename__ = "robot_financial_checkpoint_items"
    __table_args__ = (
        UniqueConstraint(
            "checkpoint_id", "account_domain", "user_id", "asset",
            name="uq_robot_checkpoint_item_domain_user_asset",
        ),
        CheckConstraint("account_domain IN ('spot','contract')", name="ck_robot_checkpoint_item_domain"),
        CheckConstraint(
            "source_count >= 0 AND accounting_transaction_count >= 0 AND outbox_event_count >= 0",
            name="ck_robot_checkpoint_item_counts",
        ),
        Index("idx_robot_checkpoint_item_checkpoint", "checkpoint_id"),
        Index("idx_robot_checkpoint_item_owner", "account_domain", "user_id", "asset"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    checkpoint_id: Mapped[str] = mapped_column(
        ForeignKey("robot_financial_checkpoints.checkpoint_id"), nullable=False
    )
    account_domain: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    source_start_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_end_id: Mapped[int] = mapped_column(Integer, nullable=False)
    range_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    range_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opening_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    closing_snapshot_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    aggregate_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_count: Mapped[int] = mapped_column(Integer, nullable=False)
    accounting_transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    outbox_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")
