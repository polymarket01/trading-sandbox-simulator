from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0029"
down_revision = "20260317_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_outbox_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("account_domain", sa.String(32), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", sa.String(128), nullable=False),
        sa.Column("accounting_transaction_id", sa.String(64), sa.ForeignKey("accounting_transactions.transaction_id")),
        sa.Column("source_ledger_entry_id", sa.String(64)),
        sa.Column("source_event_id", sa.String(64)),
        sa.Column("user_id", sa.Integer()),
        sa.Column("asset", sa.String(16)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.String(128)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1000)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.UniqueConstraint("idempotency_key", name="uq_financial_outbox_idempotency"),
        sa.UniqueConstraint("source_ledger_entry_id", name="uq_financial_outbox_source_ledger"),
        sa.CheckConstraint("status IN ('pending','processing','delivered','dead')", name="ck_financial_outbox_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_financial_outbox_attempts"),
    )
    op.create_index("idx_financial_outbox_delivery", "financial_outbox_events", ["status", "available_at", "id"])
    op.create_index("idx_financial_outbox_domain_time", "financial_outbox_events", ["account_domain", "created_at"])
    op.create_index("idx_financial_outbox_aggregate", "financial_outbox_events", ["aggregate_type", "aggregate_id"])
    op.create_index("idx_financial_outbox_source_ledger", "financial_outbox_events", ["source_ledger_entry_id"])
    op.create_index("idx_financial_outbox_source_event", "financial_outbox_events", ["source_event_id"])
    op.create_table(
        "outbox_consumer_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("receipt_id", sa.String(64), nullable=False, unique=True),
        sa.Column("consumer_name", sa.String(128), nullable=False),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("financial_outbox_events.event_id"), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("consumer_name", "event_id", name="uq_outbox_receipt_consumer_event"),
    )
    op.create_index("idx_outbox_receipt_event", "outbox_consumer_receipts", ["event_id"])
    op.create_index("idx_outbox_receipt_processed", "outbox_consumer_receipts", ["processed_at"])
    op.create_table(
        "outbox_checkpoints",
        sa.Column("checkpoint_name", sa.String(64), primary_key=True),
        sa.Column("watermark_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    event_count = int(op.get_bind().execute(sa.text("SELECT count(*) FROM financial_outbox_events")).scalar_one() or 0)
    if event_count:
        raise RuntimeError(
            "unsafe downgrade refused: durable financial outbox events exist; restore the pre-0029 backup instead"
        )
    op.drop_table("outbox_checkpoints")
    op.drop_index("idx_outbox_receipt_processed", table_name="outbox_consumer_receipts")
    op.drop_index("idx_outbox_receipt_event", table_name="outbox_consumer_receipts")
    op.drop_table("outbox_consumer_receipts")
    op.drop_index("idx_financial_outbox_source_event", table_name="financial_outbox_events")
    op.drop_index("idx_financial_outbox_source_ledger", table_name="financial_outbox_events")
    op.drop_index("idx_financial_outbox_aggregate", table_name="financial_outbox_events")
    op.drop_index("idx_financial_outbox_domain_time", table_name="financial_outbox_events")
    op.drop_index("idx_financial_outbox_delivery", table_name="financial_outbox_events")
    op.drop_table("financial_outbox_events")
