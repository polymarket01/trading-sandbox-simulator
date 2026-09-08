from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0004"
down_revision = "20260317_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_templates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("strategy_key", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), nullable=False, server_default="market"),
        sa.Column("config_schema_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("default_config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("strategy_key", name="uq_strategy_templates_key"),
    )
    op.create_table(
        "market_strategy_configs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("strategy_key", sa.String(length=32), sa.ForeignKey("strategy_templates.strategy_key"), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_by_user_id", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "strategy_key", name="uq_market_strategy_market_key"),
    )
    op.create_index("idx_market_strategy_configs_market", "market_strategy_configs", ["market_id"], unique=False)
    op.create_index("idx_market_strategy_configs_key", "market_strategy_configs", ["strategy_key"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_market_strategy_configs_key", table_name="market_strategy_configs")
    op.drop_index("idx_market_strategy_configs_market", table_name="market_strategy_configs")
    op.drop_table("market_strategy_configs")
    op.drop_table("strategy_templates")
