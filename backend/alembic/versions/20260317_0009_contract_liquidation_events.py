from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0009"
down_revision = "20260317_0008"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.create_table(
        "contract_liquidation_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=64), unique=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("position_side", sa.String(length=16), nullable=False),
        sa.Column("quantity", dec(), nullable=False, server_default="0"),
        sa.Column("entry_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("liquidation_price", dec(), nullable=False, server_default="0"),
        sa.Column("realized_pnl", dec(), nullable=False, server_default="0"),
        sa.Column("released_margin", dec(), nullable=False, server_default="0"),
        sa.Column("maintenance_margin", dec(), nullable=False, server_default="0"),
        sa.Column("margin_buffer", dec(), nullable=False, server_default="0"),
        sa.Column("risk_status", sa.String(length=32), nullable=False, server_default="liquidation_due"),
        sa.Column("reason", sa.String(length=64), nullable=False, server_default="mark_price_liquidation"),
        sa.Column("liquidated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_contract_liquidation_events_market_time",
        "contract_liquidation_events",
        ["market_id", "liquidated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_contract_liquidation_events_market_time", table_name="contract_liquidation_events")
    op.drop_table("contract_liquidation_events")
