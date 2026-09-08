from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0018"
down_revision = "20260317_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("sequence_number", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("orders", sa.Column("version", sa.Integer(), nullable=False, server_default="0"))
    op.create_index(
        "idx_orders_market_status_sequence",
        "orders",
        ["market_id", "status", "sequence_number"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_orders_market_status_sequence", table_name="orders")
    op.drop_column("orders", "version")
    op.drop_column("orders", "sequence_number")
