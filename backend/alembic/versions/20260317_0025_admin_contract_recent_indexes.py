from __future__ import annotations

from alembic import op


revision = "20260317_0025"
down_revision = "20260317_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_product_market_time "
        "ON orders (product_type, market_id, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_product_time "
        "ON orders (product_type, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_product_market_time "
        "ON trades (product_type, market_id, executed_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_trades_product_time "
        "ON trades (product_type, executed_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_trades_product_time")
    op.execute("DROP INDEX IF EXISTS idx_trades_product_market_time")
    op.execute("DROP INDEX IF EXISTS idx_orders_product_time")
    op.execute("DROP INDEX IF EXISTS idx_orders_product_market_time")
