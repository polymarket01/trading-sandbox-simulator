from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260317_0039"
down_revision = "20260317_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username_normalized", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("account_epoch", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("users", sa.Column("current_account_run_id", sa.String(length=96), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_users_username_normalized", "users", ["username_normalized"], unique=True)
    op.execute("UPDATE users SET username_normalized = LOWER(username) WHERE username_normalized IS NULL")
    op.execute("UPDATE users SET current_account_run_id = 'legacy-' || CAST(id AS VARCHAR(64)) WHERE current_account_run_id IS NULL")

    op.add_column("markets", sa.Column("paper_status", sa.String(length=32), nullable=False, server_default="TRADING"))
    op.add_column("markets", sa.Column("price_source", sa.String(length=32), nullable=False, server_default="manual"))
    op.add_column("markets", sa.Column("price_source_symbol", sa.String(length=64), nullable=True))
    op.add_column("markets", sa.Column("price_source_stale_after_seconds", sa.Integer(), nullable=False, server_default="30"))
    op.add_column("markets", sa.Column("price_protection_pct", sa.Numeric(18, 10), nullable=False, server_default="0.05"))
    op.add_column("markets", sa.Column("delist_at", sa.DateTime(timezone=True), nullable=True))

    for table in ("balances", "contract_accounts", "contract_positions", "orders", "ledger_entries", "contract_ledger_entries", "contract_funding_events", "contract_liquidation_events", "contract_adl_events"):
        op.add_column(table, sa.Column("account_run_id", sa.String(length=96), nullable=True))
    op.add_column("trades", sa.Column("account_run_id", sa.String(length=96), nullable=True))
    op.add_column("trades", sa.Column("taker_account_run_id", sa.String(length=96), nullable=True))
    op.add_column("trades", sa.Column("maker_account_run_id", sa.String(length=96), nullable=True))
    op.add_column("trades", sa.Column("global_run_id", sa.String(length=96), nullable=True))

    op.create_table(
        "paper_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("idx_paper_sessions_user_expiry", "paper_sessions", ["user_id", "expires_at"], unique=False)

    op.create_table(
        "paper_account_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=96), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("account_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "run_id", name="uq_paper_account_run_user_run"),
    )
    op.create_index("idx_paper_account_runs_user_created", "paper_account_runs", ["user_id", "created_at"], unique=False)

    op.create_table(
        "paper_assets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("icon", sa.String(length=255), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column("display_precision", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ACTIVE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )

    op.create_table(
        "paper_brand_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("exchange_name", sa.String(length=96), nullable=False, server_default="Paper Exchange"),
        sa.Column("logo_url", sa.String(length=255), nullable=True),
        sa.Column("favicon_url", sa.String(length=255), nullable=True),
        sa.Column("primary_color", sa.String(length=32), nullable=False, server_default="#22d3ee"),
        sa.Column("default_language", sa.String(length=16), nullable=False, server_default="zh-CN"),
        sa.Column("footer_text", sa.String(length=255), nullable=False, server_default="Paper Trading / 模拟交易"),
        sa.Column("paper_notice", sa.String(length=255), nullable=False, server_default="所有资产均为模拟资金，不涉及真实资金。"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "paper_system_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )

    op.create_table(
        "paper_liquidity_configs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("market_id", sa.Integer(), nullable=False),
        sa.Column("levels_per_side", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("spread_bps", sa.Numeric(18, 10), nullable=False, server_default="10"),
        sa.Column("level_spacing_bps", sa.Numeric(18, 10), nullable=False, server_default="5"),
        sa.Column("depth_quote_per_side", sa.Numeric(36, 18), nullable=False, server_default="5000000"),
        sa.Column("refresh_interval_ms", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("flow_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("flow_interval_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("flow_notional", sa.Numeric(36, 18), nullable=False, server_default="20000"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("stale_action", sa.String(length=16), nullable=False, server_default="hold"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["market_id"], ["markets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("market_id", name="uq_paper_liquidity_market"),
    )

    op.create_table(
        "paper_global_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=96), nullable=False),
        sa.Column("global_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id"),
    )

    op.execute("INSERT INTO paper_global_runs (id, run_id, global_epoch, status) VALUES (1, 'paper-global-1', 1, 'active')")

    op.create_table(
        "paper_reset_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("old_run_id", sa.String(length=96), nullable=True),
        sa.Column("new_run_id", sa.String(length=96), nullable=True),
        sa.Column("old_epoch", sa.Integer(), nullable=True),
        sa.Column("new_epoch", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_paper_reset_records_user_created", "paper_reset_records", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_paper_reset_records_user_created", table_name="paper_reset_records")
    op.drop_table("paper_reset_records")
    op.drop_table("paper_global_runs")
    op.drop_table("paper_liquidity_configs")
    op.drop_table("paper_system_settings")
    op.drop_table("paper_brand_config")
    op.drop_index("idx_paper_account_runs_user_created", table_name="paper_account_runs")
    op.drop_table("paper_account_runs")
    op.drop_index("idx_paper_sessions_user_expiry", table_name="paper_sessions")
    op.drop_table("paper_sessions")
    op.drop_table("paper_assets")
    for table, column in (
        ("trades", "global_run_id"),
        ("trades", "maker_account_run_id"),
        ("trades", "taker_account_run_id"),
        ("trades", "account_run_id"),
        ("contract_adl_events", "account_run_id"),
        ("contract_liquidation_events", "account_run_id"),
        ("contract_funding_events", "account_run_id"),
        ("contract_ledger_entries", "account_run_id"),
        ("ledger_entries", "account_run_id"),
        ("orders", "account_run_id"),
        ("contract_positions", "account_run_id"),
        ("contract_accounts", "account_run_id"),
        ("balances", "account_run_id"),
    ):
        op.drop_column(table, column)
    for column in ("delist_at", "price_protection_pct", "price_source_stale_after_seconds", "price_source_symbol", "price_source", "paper_status"):
        op.drop_column("markets", column)
    op.drop_index("ix_users_username_normalized", table_name="users")
    for column in ("last_login_at", "current_account_run_id", "account_epoch", "username_normalized"):
        op.drop_column("users", column)
