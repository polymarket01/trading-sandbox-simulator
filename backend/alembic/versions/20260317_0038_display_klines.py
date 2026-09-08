from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260317_0038"
down_revision = "20260317_0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "display_klines",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(36, 18), nullable=False),
        sa.Column("high", sa.Numeric(36, 18), nullable=False),
        sa.Column("low", sa.Numeric(36, 18), nullable=False),
        sa.Column("close", sa.Numeric(36, 18), nullable=False),
        sa.Column("volume", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("quote_volume", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="synthetic_flow"),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "market_id",
            "interval",
            "open_time",
            name="uq_display_kline_market_interval_open",
        ),
    )
    op.create_index(
        "idx_display_klines_market_interval_time",
        "display_klines",
        ["market_id", "interval", "open_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_display_klines_market_interval_time", table_name="display_klines")
    op.drop_table("display_klines")
