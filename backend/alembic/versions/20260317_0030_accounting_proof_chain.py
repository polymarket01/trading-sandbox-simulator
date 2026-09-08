from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0030"
down_revision = "20260317_0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_proof_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("checkpoint_id", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column(
            "previous_checkpoint_id",
            sa.String(64),
            sa.ForeignKey("accounting_proof_checkpoints.checkpoint_id"),
        ),
        sa.Column("previous_proof_hash", sa.String(64)),
        sa.Column("reconciliation_run_id", sa.String(64), sa.ForeignKey("reconciliation_runs.run_id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("difference_count", sa.Integer(), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("watermark_json", sa.JSON(), nullable=False),
        sa.Column("counts_json", sa.JSON(), nullable=False),
        sa.Column("business_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("accounting_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("business_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("accounting_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("proof_hash", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.UniqueConstraint("idempotency_key", name="uq_accounting_proof_idempotency"),
        sa.UniqueConstraint("sequence_no", name="uq_accounting_proof_sequence"),
        sa.UniqueConstraint("reconciliation_run_id", name="uq_accounting_proof_reconciliation_run"),
        sa.UniqueConstraint("proof_hash", name="uq_accounting_proof_hash"),
        sa.CheckConstraint("sequence_no > 0", name="ck_accounting_proof_sequence_positive"),
        sa.CheckConstraint("status IN ('passed','failed')", name="ck_accounting_proof_status"),
        sa.CheckConstraint("difference_count >= 0 AND blocking_count >= 0", name="ck_accounting_proof_counts"),
    )
    op.create_index("idx_accounting_proof_completed", "accounting_proof_checkpoints", ["completed_at"])
    op.create_index("idx_accounting_proof_status", "accounting_proof_checkpoints", ["status", "sequence_no"])
    op.execute("""
        CREATE TRIGGER trg_accounting_proof_no_update
        BEFORE UPDATE ON accounting_proof_checkpoints
        BEGIN SELECT RAISE(ABORT, 'accounting proof checkpoints are immutable'); END
    """)
    op.execute("""
        CREATE TRIGGER trg_accounting_proof_no_delete
        BEFORE DELETE ON accounting_proof_checkpoints
        BEGIN SELECT RAISE(ABORT, 'accounting proof checkpoints are immutable'); END
    """)


def downgrade() -> None:
    checkpoint_count = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM accounting_proof_checkpoints")).scalar_one() or 0
    )
    if checkpoint_count:
        raise RuntimeError(
            "unsafe downgrade refused: immutable accounting proof checkpoints exist; "
            "restore the pre-0030 backup instead"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_accounting_proof_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_accounting_proof_no_update")
    op.drop_index("idx_accounting_proof_status", table_name="accounting_proof_checkpoints")
    op.drop_index("idx_accounting_proof_completed", table_name="accounting_proof_checkpoints")
    op.drop_table("accounting_proof_checkpoints")
