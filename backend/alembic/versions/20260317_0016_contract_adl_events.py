from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0016"
down_revision = "20260317_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contract_adl_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("liquidation_event_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("position_side", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("entry_price", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("mark_price", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("execution_price", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("released_margin", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("bad_debt_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("covered_amount", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("residual_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("pnl_pct", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("effective_leverage", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("rank_score", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="executed"),
        sa.Column("reason", sa.String(length=64), nullable=False, server_default="insurance_shortfall_adl"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index("idx_contract_adl_events_liquidation", "contract_adl_events", ["liquidation_event_id", "created_at"])
    op.create_index("idx_contract_adl_events_market_time", "contract_adl_events", ["market_id", "created_at"])
    op.create_index("idx_contract_adl_events_user_time", "contract_adl_events", ["user_id", "created_at"])
    op.add_column(
        "contract_liquidation_events",
        sa.Column("adl_covered", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )
    op.add_column(
        "contract_liquidation_events",
        sa.Column("adl_residual", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )
    op.add_column(
        "contract_liquidation_events",
        sa.Column("adl_status", sa.String(length=32), nullable=False, server_default="not_required"),
    )
    op.execute(
        "UPDATE contract_liquidation_events "
        "SET adl_residual = residual_bad_debt, "
        "adl_status = CASE WHEN residual_bad_debt > 0 THEN 'pending' ELSE 'not_required' END"
    )


def downgrade() -> None:
    op.drop_column("contract_liquidation_events", "adl_status")
    op.drop_column("contract_liquidation_events", "adl_residual")
    op.drop_column("contract_liquidation_events", "adl_covered")
    op.drop_index("idx_contract_adl_events_user_time", table_name="contract_adl_events")
    op.drop_index("idx_contract_adl_events_market_time", table_name="contract_adl_events")
    op.drop_index("idx_contract_adl_events_liquidation", table_name="contract_adl_events")
    op.drop_table("contract_adl_events")
