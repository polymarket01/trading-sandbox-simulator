from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0017"
down_revision = "20260317_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contract_risk_limit_tiers",
        sa.Column("maintenance_amount", sa.Numeric(36, 18), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("contract_risk_limit_tiers", "maintenance_amount")
