from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0013"
down_revision = "20260317_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "markets",
        sa.Column("contract_trading_mode", sa.String(length=32), nullable=False, server_default="normal"),
    )


def downgrade() -> None:
    op.drop_column("markets", "contract_trading_mode")
