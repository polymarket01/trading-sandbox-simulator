from __future__ import annotations

from alembic import op


revision = "20260317_0021"
down_revision = "20260317_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_orders_status_market_user",
        "orders",
        ["status", "market_id", "user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_orders_status_market_user", table_name="orders")
