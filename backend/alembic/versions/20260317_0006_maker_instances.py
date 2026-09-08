from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0006"
down_revision = "20260317_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_maker_instances",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="stopped"),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("strategy_key", sa.String(length=32), nullable=True),
        sa.Column("started_by_user_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_metrics_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("log_path", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.ForeignKeyConstraint(["started_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id", name="uq_market_maker_instance_market"),
    )
    op.create_index("ix_market_maker_instances_symbol", "market_maker_instances", ["symbol"], unique=False)

    op.execute(
        """
        INSERT INTO market_maker_instances (market_id, symbol, status, created_at, updated_at)
        SELECT id, symbol, 'stopped', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM markets
        WHERE id NOT IN (SELECT market_id FROM market_maker_instances)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_market_maker_instances_symbol", table_name="market_maker_instances")
    op.drop_table("market_maker_instances")
