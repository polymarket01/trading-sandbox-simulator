from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0031"
down_revision = "20260317_0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "robot_financial_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("checkpoint_id", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("previous_checkpoint_id", sa.String(64), sa.ForeignKey("robot_financial_checkpoints.checkpoint_id")),
        sa.Column("previous_checkpoint_hash", sa.String(64)),
        sa.Column("reconciliation_run_id", sa.String(64), sa.ForeignKey("reconciliation_runs.run_id"), nullable=False),
        sa.Column("accounting_proof_checkpoint_id", sa.String(64), sa.ForeignKey("accounting_proof_checkpoints.checkpoint_id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("range_watermark_json", sa.JSON(), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("checkpoint_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.UniqueConstraint("idempotency_key", name="uq_robot_financial_checkpoint_idempotency"),
        sa.UniqueConstraint("sequence_no", name="uq_robot_financial_checkpoint_sequence"),
        sa.UniqueConstraint("reconciliation_run_id", name="uq_robot_financial_checkpoint_reconciliation"),
        sa.UniqueConstraint("accounting_proof_checkpoint_id", name="uq_robot_financial_checkpoint_proof"),
        sa.UniqueConstraint("checkpoint_hash", name="uq_robot_financial_checkpoint_hash"),
        sa.CheckConstraint("sequence_no > 0", name="ck_robot_financial_checkpoint_sequence"),
        sa.CheckConstraint("status IN ('passed','failed')", name="ck_robot_financial_checkpoint_status"),
    )
    op.create_index("idx_robot_financial_checkpoint_created", "robot_financial_checkpoints", ["created_at"])
    op.create_table(
        "robot_financial_checkpoint_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("item_id", sa.String(64), nullable=False, unique=True),
        sa.Column("checkpoint_id", sa.String(64), sa.ForeignKey("robot_financial_checkpoints.checkpoint_id"), nullable=False),
        sa.Column("account_domain", sa.String(16), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("source_start_id", sa.Integer(), nullable=False),
        sa.Column("source_end_id", sa.Integer(), nullable=False),
        sa.Column("range_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("range_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opening_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("closing_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("aggregate_json", sa.JSON(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("accounting_transaction_count", sa.Integer(), nullable=False),
        sa.Column("outbox_event_count", sa.Integer(), nullable=False),
        sa.Column("source_manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.UniqueConstraint("checkpoint_id", "account_domain", "user_id", "asset", name="uq_robot_checkpoint_item_domain_user_asset"),
        sa.CheckConstraint("account_domain IN ('spot','contract')", name="ck_robot_checkpoint_item_domain"),
        sa.CheckConstraint("source_count >= 0 AND accounting_transaction_count >= 0 AND outbox_event_count >= 0", name="ck_robot_checkpoint_item_counts"),
    )
    op.create_index("idx_robot_checkpoint_item_checkpoint", "robot_financial_checkpoint_items", ["checkpoint_id"])
    op.create_index("idx_robot_checkpoint_item_owner", "robot_financial_checkpoint_items", ["account_domain", "user_id", "asset"])
    for table in ("robot_financial_checkpoint_items", "robot_financial_checkpoints"):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_no_update
            BEFORE UPDATE ON {table}
            BEGIN SELECT RAISE(ABORT, 'robot financial checkpoints are immutable'); END
        """)
        op.execute(f"""
            CREATE TRIGGER trg_{table}_no_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, 'robot financial checkpoints are immutable'); END
        """)


def downgrade() -> None:
    checkpoint_count = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM robot_financial_checkpoints")).scalar_one() or 0
    )
    if checkpoint_count:
        raise RuntimeError(
            "unsafe downgrade refused: immutable robot financial checkpoints exist; "
            "restore the pre-0031 backup instead"
        )
    for table in ("robot_financial_checkpoint_items", "robot_financial_checkpoints"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
    op.drop_index("idx_robot_checkpoint_item_owner", table_name="robot_financial_checkpoint_items")
    op.drop_index("idx_robot_checkpoint_item_checkpoint", table_name="robot_financial_checkpoint_items")
    op.drop_table("robot_financial_checkpoint_items")
    op.drop_index("idx_robot_financial_checkpoint_created", table_name="robot_financial_checkpoints")
    op.drop_table("robot_financial_checkpoints")
