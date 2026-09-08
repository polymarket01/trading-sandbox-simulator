from __future__ import annotations

from alembic import op


revision = "20260317_0022"
down_revision = "20260317_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_orders_user_market_client_status",
        "orders",
        ["user_id", "market_id", "client_order_id", "status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_orders_user_market_client_status", table_name="orders")
