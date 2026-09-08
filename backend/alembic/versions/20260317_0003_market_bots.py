from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0003"
down_revision = "20260317_0002"
branch_labels = None
depends_on = None


def dec(precision: int = 36, scale: int = 18) -> sa.Numeric:
    return sa.Numeric(precision, scale)


def upgrade() -> None:
    op.add_column("markets", sa.Column("reference_price", dec(), nullable=True))
    op.create_table(
        "market_bot_accounts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("market_id", sa.Integer(), sa.ForeignKey("markets.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bot_label", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="maker"),
        sa.Column("strategy_role", sa.String(length=32)),
        sa.Column("initial_quote_amount", dec(), nullable=False),
        sa.Column("initial_base_amount", dec(), nullable=False),
        sa.Column("initial_base_notional", dec(), nullable=False),
        sa.Column("reference_price", dec(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("market_id", "user_id", name="uq_market_bot_market_user"),
        sa.UniqueConstraint("market_id", "bot_label", name="uq_market_bot_market_label"),
    )
    op.create_index("idx_market_bot_accounts_market", "market_bot_accounts", ["market_id"], unique=False)
    op.create_index("idx_market_bot_accounts_user", "market_bot_accounts", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("idx_market_bot_accounts_user", table_name="market_bot_accounts")
    op.drop_index("idx_market_bot_accounts_market", table_name="market_bot_accounts")
    op.drop_table("market_bot_accounts")
    op.drop_column("markets", "reference_price")
