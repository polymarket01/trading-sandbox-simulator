from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0014"
down_revision = "20260317_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contract_ledger_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=True),
        sa.Column("margin_asset", sa.String(length=16), nullable=False, server_default="USDT"),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("wallet_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("wallet_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("available_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("available_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("used_margin_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("used_margin_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("realized_pnl_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("realized_pnl_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("total_fees_before", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("total_fees_after", sa.Numeric(36, 18), nullable=False, server_default="0"),
        sa.Column("related_order_id", sa.String(length=64), nullable=True),
        sa.Column("related_trade_id", sa.String(length=64), nullable=True),
        sa.Column("related_event_id", sa.String(length=64), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_id"),
    )
    op.create_index("idx_contract_ledger_user_asset_time", "contract_ledger_entries", ["user_id", "margin_asset", "created_at"])
    op.create_index("idx_contract_ledger_market_time", "contract_ledger_entries", ["market_id", "created_at"])


def downgrade() -> None:
    op.drop_index("idx_contract_ledger_market_time", table_name="contract_ledger_entries")
    op.drop_index("idx_contract_ledger_user_asset_time", table_name="contract_ledger_entries")
    op.drop_table("contract_ledger_entries")
