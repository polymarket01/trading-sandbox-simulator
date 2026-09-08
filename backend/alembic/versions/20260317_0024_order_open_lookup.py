from __future__ import annotations

from alembic import op


revision = "20260317_0024"
down_revision = "20260317_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_user_market_product_status_time "
        "ON orders (user_id, market_id, product_type, status, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orders_user_market_product_status_time")
