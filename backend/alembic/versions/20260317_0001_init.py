from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0001"
down_revision = None
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("api_key", sa.String(length=128)),
        sa.Column("api_secret_hash", sa.String(length=255)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("username"),
        sa.UniqueConstraint("api_key"),
    )
    op.create_table(
        "markets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("base_asset", sa.String(length=16), nullable=False),
        sa.Column("quote_asset", sa.String(length=16), nullable=False),
        sa.Column("price_tick", dec(), nullable=False),
        sa.Column("qty_step", dec(), nullable=False),
        sa.Column("min_qty", dec(), nullable=False),
        sa.Column("min_notional", dec(), nullable=False),
        sa.Column("price_precision", sa.Integer(), nullable=False),
        sa.Column("qty_precision", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_maker_fee_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("default_taker_fee_rate", sa.Numeric(18, 10), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("symbol"),
    )
    op.create_table(
        "balances",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("available", dec(), nullable=False, server_default="0"),
        sa.Column("frozen", dec(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "asset", name="uq_balance_user_asset"),
    )
    op.create_table(
        "fee_profiles",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id")),
        sa.Column("maker_fee_rate", sa.Numeric(18, 10), nullable=False),
        sa.Column("taker_fee_rate", sa.Numeric(18, 10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "market_id", name="uq_fee_profile_user_market"),
    )
    op.create_table(
        "orders",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("client_order_id", sa.String(length=64)),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("tif", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("price", dec()),
        sa.Column("quantity", dec(), nullable=False),
        sa.Column("filled_quantity", dec(), nullable=False, server_default="0"),
        sa.Column("avg_price", dec()),
        sa.Column("notional", dec()),
        sa.Column("remaining_quantity", dec(), nullable=False),
        sa.Column("reference_price", dec()),
        sa.Column("protection_bps", sa.Integer()),
        sa.Column("max_price", dec()),
        sa.Column("min_price", dec()),
        sa.Column("reject_reason", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("order_id"),
    )
    op.create_index("idx_orders_user_market", "orders", ["user_id", "market_id", "created_at"], unique=False)
    op.create_index("idx_orders_market_status", "orders", ["market_id", "status", "created_at"], unique=False)
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trade_id", sa.String(length=64), nullable=False),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("price", dec(), nullable=False),
        sa.Column("quantity", dec(), nullable=False),
        sa.Column("quote_amount", dec(), nullable=False),
        sa.Column("taker_order_id", sa.String(length=64), nullable=False),
        sa.Column("maker_order_id", sa.String(length=64), nullable=False),
        sa.Column("taker_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("maker_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("taker_side", sa.String(length=8), nullable=False),
        sa.Column("maker_fee", dec(), nullable=False, server_default="0"),
        sa.Column("taker_fee", dec(), nullable=False, server_default="0"),
        sa.Column("fee_asset_maker", sa.String(length=16)),
        sa.Column("fee_asset_taker", sa.String(length=16)),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("trade_id"),
    )
    op.create_index("idx_trades_market_time", "trades", ["market_id", "executed_at"], unique=False)
    op.create_index("idx_trades_user_time", "trades", ["taker_user_id", "executed_at"], unique=False)
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("amount", dec(), nullable=False),
        sa.Column("balance_before", dec(), nullable=False),
        sa.Column("balance_after", dec(), nullable=False),
        sa.Column("available_before", dec(), nullable=False),
        sa.Column("available_after", dec(), nullable=False),
        sa.Column("frozen_before", dec(), nullable=False),
        sa.Column("frozen_after", dec(), nullable=False),
        sa.Column("related_order_id", sa.String(length=64)),
        sa.Column("related_trade_id", sa.String(length=64)),
        sa.Column("note", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entry_id"),
    )
    op.create_index("idx_ledger_user_asset_time", "ledger_entries", ["user_id", "asset", "created_at"], unique=False)
    op.create_table(
        "klines",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("interval", sa.String(length=8), nullable=False),
        sa.Column("open_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("close_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", dec(), nullable=False),
        sa.Column("high", dec(), nullable=False),
        sa.Column("low", dec(), nullable=False),
        sa.Column("close", dec(), nullable=False),
        sa.Column("volume", dec(), nullable=False, server_default="0"),
        sa.Column("quote_volume", dec(), nullable=False, server_default="0"),
        sa.Column("trade_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("market_id", "interval", "open_time", name="uq_kline_market_interval_open"),
    )
    op.create_index("idx_klines_market_interval_time", "klines", ["market_id", "interval", "open_time"], unique=False)
    op.create_table(
        "reset_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("asset", sa.String(length=16), nullable=False),
        sa.Column("amount", dec(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", "user_id", "asset", name="uq_reset_template_name_user_asset"),
    )


def downgrade() -> None:
    op.drop_table("reset_templates")
    op.drop_index("idx_klines_market_interval_time", table_name="klines")
    op.drop_table("klines")
    op.drop_index("idx_ledger_user_asset_time", table_name="ledger_entries")
    op.drop_table("ledger_entries")
    op.drop_index("idx_trades_user_time", table_name="trades")
    op.drop_index("idx_trades_market_time", table_name="trades")
    op.drop_table("trades")
    op.drop_index("idx_orders_market_status", table_name="orders")
    op.drop_index("idx_orders_user_market", table_name="orders")
    op.drop_table("orders")
    op.drop_table("fee_profiles")
    op.drop_table("balances")
    op.drop_table("markets")
    op.drop_table("users")
