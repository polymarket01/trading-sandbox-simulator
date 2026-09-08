from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0010"
down_revision = "20260317_0009"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.create_table(
        "contract_risk_limit_tiers",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("notional_floor", dec(), nullable=False, server_default="0"),
        sa.Column("notional_cap", dec(), nullable=True),
        sa.Column("max_leverage", sa.Numeric(18, 6), nullable=False, server_default="1"),
        sa.Column("maintenance_margin_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "tier", name="uq_contract_risk_tier_market_tier"),
    )
    op.create_index(
        "idx_contract_risk_tiers_market_floor",
        "contract_risk_limit_tiers",
        ["market_id", "notional_floor"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO contract_risk_limit_tiers
                (market_id, tier, notional_floor, notional_cap, max_leverage, maintenance_margin_rate)
            SELECT id, 1, 0, NULL, max_leverage, maintenance_margin_rate
            FROM markets
            WHERE product_type = 'PERP'
            """
        )
    )


def downgrade() -> None:
    op.drop_index("idx_contract_risk_tiers_market_floor", table_name="contract_risk_limit_tiers")
    op.drop_table("contract_risk_limit_tiers")
