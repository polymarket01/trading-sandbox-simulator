from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0032"
down_revision = "20260317_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_evidence_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_key", sa.String(64), nullable=False),
        sa.Column("observation_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reconciliation_run_id", sa.String(64), sa.ForeignKey("reconciliation_runs.run_id")),
        sa.Column(
            "accounting_proof_checkpoint_id",
            sa.String(64),
            sa.ForeignKey("accounting_proof_checkpoints.checkpoint_id"),
        ),
        sa.Column(
            "robot_checkpoint_id",
            sa.String(64),
            sa.ForeignKey("robot_financial_checkpoints.checkpoint_id"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(96)),
        sa.Column("last_error_message", sa.String(500)),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("observation_date", name="uq_accounting_evidence_observation_date"),
        sa.UniqueConstraint("run_key", name="uq_accounting_evidence_run_key"),
        sa.UniqueConstraint("reconciliation_run_id", name="uq_accounting_evidence_reconciliation"),
        sa.UniqueConstraint("accounting_proof_checkpoint_id", name="uq_accounting_evidence_proof"),
        sa.UniqueConstraint("robot_checkpoint_id", name="uq_accounting_evidence_robot_checkpoint"),
        sa.CheckConstraint(
            "status IN ('running','passed','failed')",
            name="ck_accounting_evidence_status",
        ),
        sa.CheckConstraint("attempt_count > 0", name="ck_accounting_evidence_attempt_count"),
    )
    op.create_index(
        "idx_accounting_evidence_status_date",
        "accounting_evidence_runs",
        ["status", "observation_date"],
    )


def downgrade() -> None:
    linked_count = int(
        op.get_bind().execute(sa.text("""
            SELECT count(*)
            FROM accounting_evidence_runs
            WHERE reconciliation_run_id IS NOT NULL
               OR accounting_proof_checkpoint_id IS NOT NULL
               OR robot_checkpoint_id IS NOT NULL
        """)).scalar_one() or 0
    )
    if linked_count:
        raise RuntimeError(
            "unsafe downgrade refused: accounting evidence runs reference immutable proof rows; "
            "restore the pre-0032 backup instead"
        )
    op.drop_index("idx_accounting_evidence_status_date", table_name="accounting_evidence_runs")
    op.drop_table("accounting_evidence_runs")

