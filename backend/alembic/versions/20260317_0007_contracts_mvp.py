from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0007"
down_revision = "20260317_0006"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.add_column("markets", sa.Column("product_type", sa.String(length=16), nullable=False, server_default="SPOT"))
    op.add_column("markets", sa.Column("margin_asset", sa.String(length=16), nullable=True))
    op.add_column("markets", sa.Column("max_leverage", sa.Numeric(18, 6), nullable=False, server_default="1"))
    op.add_column("markets", sa.Column("default_leverage", sa.Numeric(18, 6), nullable=False, server_default="1"))
    op.add_column("markets", sa.Column("maintenance_margin_rate", sa.Numeric(18, 10), nullable=False, server_default="0"))
    op.add_column("markets", sa.Column("funding_rate", sa.Numeric(18, 10), nullable=False, server_default="0"))
    op.add_column("markets", sa.Column("funding_interval_hours", sa.Integer(), nullable=False, server_default="8"))

    op.add_column("orders", sa.Column("product_type", sa.String(length=16), nullable=False, server_default="SPOT"))
    op.add_column("orders", sa.Column("position_action", sa.String(length=16), nullable=True))
    op.add_column("orders", sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("orders", sa.Column("leverage", sa.Numeric(18, 6), nullable=True))

    op.add_column("trades", sa.Column("product_type", sa.String(length=16), nullable=False, server_default="SPOT"))
    op.add_column("trades", sa.Column("taker_position_action", sa.String(length=16), nullable=True))
    op.add_column("trades", sa.Column("maker_position_action", sa.String(length=16), nullable=True))
    op.add_column("trades", sa.Column("taker_realized_pnl", dec(), nullable=False, server_default="0"))
    op.add_column("trades", sa.Column("maker_realized_pnl", dec(), nullable=False, server_default="0"))

    op.create_table(
        "contract_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("margin_asset", sa.String(length=16), nullable=False, server_default="USDT"),
        sa.Column("wallet_balance", dec(), nullable=False, server_default="0"),
        sa.Column("available_margin", dec(), nullable=False, server_default="0"),
        sa.Column("used_margin", dec(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", dec(), nullable=False, server_default="0"),
        sa.Column("realized_pnl", dec(), nullable=False, server_default="0"),
        sa.Column("total_fees", dec(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "margin_asset", name="uq_contract_account_user_margin_asset"),
    )
    op.create_table(
        "contract_positions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False, server_default="flat"),
        sa.Column("quantity", dec(), nullable=False, server_default="0"),
        sa.Column("entry_price", dec(), nullable=False, server_default="0"),
        sa.Column("mark_price", dec(), nullable=False, server_default="0"),
        sa.Column("liquidation_price", dec(), nullable=False, server_default="0"),
        sa.Column("leverage", sa.Numeric(18, 6), nullable=False, server_default="1"),
        sa.Column("margin_mode", sa.String(length=16), nullable=False, server_default="isolated"),
        sa.Column("isolated_margin", dec(), nullable=False, server_default="0"),
        sa.Column("maintenance_margin", dec(), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", dec(), nullable=False, server_default="0"),
        sa.Column("realized_pnl", dec(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "market_id", name="uq_contract_position_user_market"),
    )
    op.create_index("idx_contract_positions_market", "contract_positions", ["market_id", "side"], unique=False)
    op.create_table(
        "contract_user_settings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("leverage", sa.Numeric(18, 6), nullable=False, server_default="1"),
        sa.Column("margin_mode", sa.String(length=16), nullable=False, server_default="isolated"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "market_id", name="uq_contract_user_setting_user_market"),
    )


def downgrade() -> None:
    op.drop_table("contract_user_settings")
    op.drop_index("idx_contract_positions_market", table_name="contract_positions")
    op.drop_table("contract_positions")
    op.drop_table("contract_accounts")
    op.drop_column("trades", "maker_realized_pnl")
    op.drop_column("trades", "taker_realized_pnl")
    op.drop_column("trades", "maker_position_action")
    op.drop_column("trades", "taker_position_action")
    op.drop_column("trades", "product_type")
    op.drop_column("orders", "leverage")
    op.drop_column("orders", "reduce_only")
    op.drop_column("orders", "position_action")
    op.drop_column("orders", "product_type")
    op.drop_column("markets", "funding_interval_hours")
    op.drop_column("markets", "funding_rate")
    op.drop_column("markets", "maintenance_margin_rate")
    op.drop_column("markets", "default_leverage")
    op.drop_column("markets", "max_leverage")
    op.drop_column("markets", "margin_asset")
    op.drop_column("markets", "product_type")
