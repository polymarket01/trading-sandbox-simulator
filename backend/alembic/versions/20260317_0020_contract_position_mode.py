from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0020"
down_revision = "20260317_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contract_user_settings",
        sa.Column("position_mode", sa.String(length=16), nullable=False, server_default="one_way"),
    )


def downgrade() -> None:
    op.drop_column("contract_user_settings", "position_mode")
