from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0012"
down_revision = "20260317_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contract_funding_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("locked_by", sa.String(length=96), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settlement_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "funding_time", name="uq_contract_funding_job_market_time"),
    )
    op.create_index(
        "idx_contract_funding_jobs_market_time",
        "contract_funding_jobs",
        ["market_id", "funding_time"],
        unique=False,
    )
    op.create_index(
        "idx_contract_funding_jobs_status_retry",
        "contract_funding_jobs",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO contract_funding_jobs (
            job_id,
            market_id,
            funding_time,
            status,
            attempt_count,
            max_attempts,
            locked_by,
            locked_at,
            heartbeat_at,
            settlement_id,
            last_error,
            started_at,
            completed_at,
            created_at,
            updated_at
        )
        SELECT
            'fj_backfill_' || CAST(id AS VARCHAR),
            market_id,
            funding_time,
            CASE
                WHEN status IN ('settled', 'failed') THEN status
                ELSE 'pending'
            END,
            CASE
                WHEN status IN ('settled', 'failed') THEN 1
                ELSE 0
            END,
            3,
            'migration-backfill',
            started_at,
            completed_at,
            settlement_id,
            error_message,
            started_at,
            completed_at,
            created_at,
            updated_at
        FROM contract_funding_settlements
        WHERE NOT EXISTS (
            SELECT 1
            FROM contract_funding_jobs
            WHERE contract_funding_jobs.market_id = contract_funding_settlements.market_id
              AND contract_funding_jobs.funding_time = contract_funding_settlements.funding_time
        )
        """
    )


def downgrade() -> None:
    op.drop_index("idx_contract_funding_jobs_status_retry", table_name="contract_funding_jobs")
    op.drop_index("idx_contract_funding_jobs_market_time", table_name="contract_funding_jobs")
    op.drop_table("contract_funding_jobs")
