from __future__ import annotations

from alembic import op


revision = "20260317_0034"
down_revision = "20260317_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This partial index matches the exact DB-to-engine rebuild predicate and
    # preserves price-time priority without sorting the historical order table.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_live_book_recovery "
        "ON orders (market_id, product_type, sequence_number, created_at, id) "
        "WHERE type = 'limit' AND tif = 'gtc' "
        "AND status IN ('new', 'partially_filled') AND remaining_quantity > 0"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_orders_live_book_recovery")
