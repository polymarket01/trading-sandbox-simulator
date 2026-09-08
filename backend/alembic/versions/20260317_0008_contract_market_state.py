from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0008"
down_revision = "20260317_0007"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.add_column("markets", sa.Column("index_price_source", sa.String(length=32), nullable=False, server_default="binance"))
    op.add_column("markets", sa.Column("mark_price_mode", sa.String(length=32), nullable=False, server_default="orderbook"))
    op.add_column("markets", sa.Column("funding_rate_mode", sa.String(length=32), nullable=False, server_default="binance"))
    op.add_column("markets", sa.Column("funding_interest_rate", sa.Numeric(18, 10), nullable=False, server_default="0.0001"))
    op.add_column("markets", sa.Column("funding_clamp_rate", sa.Numeric(18, 10), nullable=False, server_default="0.0005"))
    op.add_column("markets", sa.Column("funding_cap_rate", sa.Numeric(18, 10), nullable=False, server_default="0.02"))
    op.add_column("markets", sa.Column("funding_impact_notional", dec(), nullable=False, server_default="25000"))

    op.create_table(
        "contract_market_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("index_symbol", sa.String(length=32), nullable=False),
        sa.Column("index_source", sa.String(length=32), nullable=False, server_default="binance"),
        sa.Column("index_price", dec(), nullable=False, server_default="0"),
        sa.Column("external_mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_source", sa.String(length=32), nullable=False, server_default="orderbook"),
        sa.Column("local_mid_price", dec(), nullable=False, server_default="0"),
        sa.Column("local_last_price", dec(), nullable=False, server_default="0"),
        sa.Column("impact_bid_price", dec(), nullable=False, server_default="0"),
        sa.Column("impact_ask_price", dec(), nullable=False, server_default="0"),
        sa.Column("premium_index", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("funding_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("funding_rate_mode", sa.String(length=32), nullable=False, server_default="binance"),
        sa.Column("interest_rate", sa.Numeric(18, 10), nullable=False, server_default="0.0001"),
        sa.Column("funding_clamp_rate", sa.Numeric(18, 10), nullable=False, server_default="0.0005"),
        sa.Column("funding_cap_rate", sa.Numeric(18, 10), nullable=False, server_default="0.02"),
        sa.Column("impact_notional", dec(), nullable=False, server_default="25000"),
        sa.Column("next_funding_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_status", sa.String(length=32), nullable=False, server_default="fallback"),
        sa.Column("source_message", sa.String(length=512), nullable=True),
        sa.Column("external_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", name="uq_contract_market_state_market"),
    )
    op.create_table(
        "contract_funding_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(length=64), unique=True, nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("position_side", sa.String(length=16), nullable=False),
        sa.Column("quantity", dec(), nullable=False, server_default="0"),
        sa.Column("index_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("funding_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("amount", dec(), nullable=False, server_default="0"),
        sa.Column("funding_rate_mode", sa.String(length=32), nullable=False, server_default="formula"),
        sa.Column("funding_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_contract_funding_events_market_time", "contract_funding_events", ["market_id", "funding_time"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_contract_funding_events_market_time", table_name="contract_funding_events")
    op.drop_table("contract_funding_events")
    op.drop_table("contract_market_states")
    op.drop_column("markets", "funding_impact_notional")
    op.drop_column("markets", "funding_cap_rate")
    op.drop_column("markets", "funding_clamp_rate")
    op.drop_column("markets", "funding_interest_rate")
    op.drop_column("markets", "funding_rate_mode")
    op.drop_column("markets", "mark_price_mode")
    op.drop_column("markets", "index_price_source")
