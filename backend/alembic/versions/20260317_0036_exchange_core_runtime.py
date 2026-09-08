from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0036"
down_revision = "20260317_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing domain events remain valid.  The nullable command columns allow
    # old rows to coexist with the new command journal without rewriting facts.
    columns = (
        ("command_id", sa.String(128)),
        ("exchange_sequence", sa.BigInteger()),
        ("account_id", sa.BigInteger()),
        ("command_type", sa.String(64)),
        ("logical_timestamp", sa.BigInteger()),
        ("config_version", sa.String(128)),
        ("payload_hash", sa.String(64)),
        ("durability_mode", sa.String(32)),
        ("sink_class", sa.String(16), {"server_default": "critical", "nullable": False}),
    )
    for item in columns:
        name, column_type, *options = item
        kwargs = options[0] if options else {}
        op.add_column("domain_event_log", sa.Column(name, column_type, **kwargs))
    op.create_index("idx_domain_event_log_exchange_sequence", "domain_event_log", ["exchange_sequence"])
    op.create_index("idx_domain_event_log_command_id", "domain_event_log", ["command_id"], unique=True)

    op.create_table(
        "exchange_watermark",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ingress_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("matched_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("durable_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("materialized_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("published_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("critical_materialized_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("non_critical_materialized_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="HEALTHY"),
        sa.Column("last_error", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO exchange_watermark (id, updated_at) VALUES (1, CURRENT_TIMESTAMP)")

    op.create_table(
        "exchange_snapshot_record",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("snapshot_seq", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("config_version", sa.String(128), nullable=False, server_default="runtime"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_exchange_snapshot_seq", "exchange_snapshot_record", ["snapshot_seq"])

    op.create_table(
        "journal_truncation_checkpoint",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("critical_materializer_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("outbox_receipt_seq", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reconciliation_run_id", sa.String(128)),
        sa.Column("reconciliation_passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("snapshot_hash_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("backup_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("proof_hash", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("INSERT INTO journal_truncation_checkpoint (id, updated_at) VALUES (1, CURRENT_TIMESTAMP)")


def downgrade() -> None:
    op.drop_table("journal_truncation_checkpoint")
    op.drop_index("idx_exchange_snapshot_seq", table_name="exchange_snapshot_record")
    op.drop_table("exchange_snapshot_record")
    op.drop_table("exchange_watermark")
    op.drop_index("idx_domain_event_log_command_id", table_name="domain_event_log")
    op.drop_index("idx_domain_event_log_exchange_sequence", table_name="domain_event_log")
    for name in (
        "sink_class",
        "durability_mode",
        "payload_hash",
        "config_version",
        "logical_timestamp",
        "command_type",
        "account_id",
        "exchange_sequence",
        "command_id",
    ):
        op.drop_column("domain_event_log", name)
