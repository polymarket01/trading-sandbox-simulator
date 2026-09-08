from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0033"
down_revision = "20260317_0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "financial_outbox_replay_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("replay_id", sa.String(64), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(191), nullable=False),
        sa.Column("event_id", sa.String(64), sa.ForeignKey("financial_outbox_events.event_id"), nullable=False),
        sa.Column("attempts_before", sa.Integer(), nullable=False),
        sa.Column("before_status", sa.String(16), nullable=False),
        sa.Column("after_status", sa.String(16), nullable=False),
        sa.Column("reconciliation_run_id", sa.String(64), sa.ForeignKey("reconciliation_runs.run_id"), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("prior_last_error", sa.String(1000)),
        sa.Column("event_payload_hash", sa.String(64), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_replay_idempotency"),
        sa.UniqueConstraint("event_id", "attempts_before", name="uq_outbox_replay_event_attempt"),
        sa.CheckConstraint("attempts_before > 0", name="ck_outbox_replay_attempts_positive"),
        sa.CheckConstraint("before_status = 'dead' AND after_status = 'pending'", name="ck_outbox_replay_transition"),
    )
    op.create_index("idx_outbox_replay_event_time", "financial_outbox_replay_requests", ["event_id", "requested_at"])
    op.create_index("idx_outbox_replay_actor_time", "financial_outbox_replay_requests", ["actor_user_id", "requested_at"])
    op.create_index("idx_outbox_replay_reconciliation", "financial_outbox_replay_requests", ["reconciliation_run_id"])
    op.execute("""
        CREATE TRIGGER trg_financial_outbox_replay_no_update
        BEFORE UPDATE ON financial_outbox_replay_requests
        BEGIN SELECT RAISE(ABORT, 'financial outbox replay requests are immutable'); END
    """)
    op.execute("""
        CREATE TRIGGER trg_financial_outbox_replay_no_delete
        BEFORE DELETE ON financial_outbox_replay_requests
        BEGIN SELECT RAISE(ABORT, 'financial outbox replay requests are immutable'); END
    """)
    op.execute("""
        CREATE TRIGGER trg_financial_outbox_fact_fields_immutable
        BEFORE UPDATE OF
            event_id, idempotency_key, event_type, account_domain,
            aggregate_type, aggregate_id, accounting_transaction_id,
            source_ledger_entry_id, source_event_id, user_id, asset,
            payload_json, occurred_at, created_at, schema_version
        ON financial_outbox_events
        BEGIN SELECT RAISE(ABORT, 'financial outbox event facts are immutable'); END
    """)


def downgrade() -> None:
    replay_count = int(
        op.get_bind().execute(sa.text("SELECT count(*) FROM financial_outbox_replay_requests")).scalar_one() or 0
    )
    if replay_count:
        raise RuntimeError(
            "unsafe downgrade refused: immutable financial outbox replay requests exist; "
            "restore the pre-0033 backup instead"
        )
    op.execute("DROP TRIGGER IF EXISTS trg_financial_outbox_fact_fields_immutable")
    op.execute("DROP TRIGGER IF EXISTS trg_financial_outbox_replay_no_delete")
    op.execute("DROP TRIGGER IF EXISTS trg_financial_outbox_replay_no_update")
    op.drop_index("idx_outbox_replay_reconciliation", table_name="financial_outbox_replay_requests")
    op.drop_index("idx_outbox_replay_actor_time", table_name="financial_outbox_replay_requests")
    op.drop_index("idx_outbox_replay_event_time", table_name="financial_outbox_replay_requests")
    op.drop_table("financial_outbox_replay_requests")
