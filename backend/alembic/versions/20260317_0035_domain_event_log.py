from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0035"
down_revision = "20260317_0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domain_event_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("product_type", sa.String(16), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
    )
    op.create_index("idx_domain_event_log_pending", "domain_event_log", ["state", "id"])
    op.create_index("idx_domain_event_log_symbol_time", "domain_event_log", ["symbol", "created_at"])
    op.create_index("idx_domain_event_log_kind_time", "domain_event_log", ["kind", "created_at"])
    op.create_table(
        "domain_event_watermark",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("materialized_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        """
        INSERT INTO domain_event_watermark (id, materialized_seq, updated_at)
        VALUES (1, 0, CURRENT_TIMESTAMP)
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_domain_event_log_immutable
        BEFORE UPDATE ON domain_event_log
        FOR EACH ROW
        WHEN OLD.product_type <> NEW.product_type
          OR OLD.symbol <> NEW.symbol
          OR OLD.user_id <> NEW.user_id
          OR OLD.kind <> NEW.kind
          OR OLD.payload_json <> NEW.payload_json
          OR OLD.created_at <> NEW.created_at
        BEGIN
          SELECT RAISE(ABORT, 'domain event log payload is immutable');
        END
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_domain_event_log_immutable")
    op.drop_index("idx_domain_event_log_pending", table_name="domain_event_log")
    op.drop_index("idx_domain_event_log_symbol_time", table_name="domain_event_log")
    op.drop_index("idx_domain_event_log_kind_time", table_name="domain_event_log")
    op.drop_table("domain_event_log")
    op.drop_table("domain_event_watermark")
