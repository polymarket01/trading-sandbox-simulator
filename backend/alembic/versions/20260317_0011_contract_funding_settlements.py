from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0011"
down_revision = "20260317_0010"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.create_table(
        "contract_funding_settlements",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("funding_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("funding_rate_mode", sa.String(length=32), nullable=False, server_default="formula"),
        sa.Column("index_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("settled_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_amount", dec(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="settling"),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "funding_time", name="uq_contract_funding_settlement_market_time"),
    )
    op.create_index(
        "idx_contract_funding_settlements_market_time",
        "contract_funding_settlements",
        ["market_id", "funding_time"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_contract_funding_settlements_market_time", table_name="contract_funding_settlements")
    op.drop_table("contract_funding_settlements")
