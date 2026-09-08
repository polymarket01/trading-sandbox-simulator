from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0015"
down_revision = "20260317_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contract_insurance_funds",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("margin_asset", sa.String(length=16), nullable=False, server_default="USDT"),
        sa.Column("balance", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("margin_asset", name="uq_contract_insurance_fund_margin_asset"),
    )
    op.create_table(
        "contract_insurance_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("margin_asset", sa.String(length=16), nullable=False, server_default="USDT"),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("balance_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("balance_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("residual_bad_debt", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("related_liquidation_event_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("idx_contract_insurance_events_asset_time", "contract_insurance_events", ["margin_asset", "created_at"])
    op.create_index("idx_contract_insurance_events_market_time", "contract_insurance_events", ["market_id", "created_at"])
    op.add_column(
        "contract_liquidation_events",
        sa.Column("bankruptcy_price", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )
    op.add_column(
        "contract_liquidation_events",
        sa.Column("insurance_covered", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )
    op.add_column(
        "contract_liquidation_events",
        sa.Column("residual_bad_debt", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("contract_liquidation_events", "residual_bad_debt")
    op.drop_column("contract_liquidation_events", "insurance_covered")
    op.drop_column("contract_liquidation_events", "bankruptcy_price")
    op.drop_index("idx_contract_insurance_events_market_time", table_name="contract_insurance_events")
    op.drop_index("idx_contract_insurance_events_asset_time", table_name="contract_insurance_events")
    op.drop_table("contract_insurance_events")
    op.drop_table("contract_insurance_funds")
