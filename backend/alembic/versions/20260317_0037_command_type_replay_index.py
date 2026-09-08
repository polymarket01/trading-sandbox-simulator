from __future__ import annotations

from alembic import op


revision = "20260317_0037"
down_revision = "20260317_0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Startup replay scans QUOTE_SET_REPLACE commands in event-id order.  With a
    # multi-hundred-thousand-row journal this must use an index, not a full
    # table scan that also JSON-decodes every payload.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_domain_event_log_command_type_id "
        "ON domain_event_log (command_type, id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_domain_event_log_command_type_id")
