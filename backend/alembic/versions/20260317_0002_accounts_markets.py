from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "markets",
        sa.Column("market_type", sa.String(length=32), nullable=False, server_default="listed"),
    )
    op.execute("UPDATE markets SET market_type = 'mainstream' WHERE symbol = 'BTCUSDT'")


def downgrade() -> None:
    op.drop_column("markets", "market_type")
    op.drop_column("users", "password_hash")
