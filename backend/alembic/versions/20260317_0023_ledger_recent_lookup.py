from __future__ import annotations

from alembic import op


revision = "20260317_0023"
down_revision = "20260317_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_user_time "
        "ON ledger_entries (user_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_ledger_user_time")
