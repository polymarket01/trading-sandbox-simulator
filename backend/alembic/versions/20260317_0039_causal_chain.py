from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260317_0041"
down_revision = "20260317_0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "causal_command_journal",
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=True),
        sa.Column("command_type", sa.String(64), nullable=False),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("account_domain", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=True),
        sa.Column("market_id", sa.String(64), nullable=True),
        sa.Column("product_type", sa.String(16), nullable=False),
        sa.Column("epoch", sa.String(128), nullable=False),
        sa.Column("command_sequence", sa.BigInteger(), nullable=False),
        sa.Column("logical_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("rules_version", sa.String(128), nullable=False),
        sa.Column("risk_version", sa.String(128), nullable=False),
        sa.Column("fee_version", sa.String(128), nullable=False),
        sa.Column("config_version", sa.String(128), nullable=False),
        sa.Column("strategy_instance", sa.String(128), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("priority_class", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="RECEIVED"),
        sa.Column("ack_stage", sa.String(32), nullable=False, server_default="RECEIVED"),
        sa.Column("execution_id", sa.String(128), nullable=True),
        sa.Column("result_hash", sa.String(64), nullable=True),
        sa.Column("reject_code", sa.String(128), nullable=True),
        sa.Column("reject_stage", sa.String(64), nullable=True),
        sa.Column("unknown_reason", sa.String(512), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("command_id"),
    )
    op.create_index(
        "idx_causal_command_status_updated",
        "causal_command_journal",
        ["status", "updated_at"],
    )
    op.create_index(
        "idx_causal_command_account_created",
        "causal_command_journal",
        ["account_id", "created_at"],
    )
    op.create_index(
        "idx_causal_command_client_order",
        "causal_command_journal",
        ["account_id", "client_order_id"],
    )

    op.create_table(
        "causal_execution_bundle",
        sa.Column("execution_id", sa.String(128), nullable=False),
        sa.Column("command_id", sa.String(128), nullable=False),
        sa.Column("epoch", sa.String(128), nullable=False),
        sa.Column("execution_sequence", sa.BigInteger(), nullable=False),
        sa.Column("priority_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("book_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rejected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("result_hash", sa.String(64), nullable=False),
        sa.Column("bundle_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="DURABLE"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("durable_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("materialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("execution_id"),
    )
    op.create_index("idx_causal_execution_command", "causal_execution_bundle", ["command_id"])
    op.create_index("idx_causal_execution_sequence", "causal_execution_bundle", ["execution_sequence"])
    op.create_index(
        "idx_causal_execution_state",
        "causal_execution_bundle",
        ["state", "created_at"],
    )

    op.create_table(
        "causal_watermark",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("epoch", sa.String(128), nullable=False),
        sa.Column("ingress_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("command_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("priority_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("matched_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("execution_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("settlement_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ledger_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("durable_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("materialized_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("published_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="STARTING"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO causal_watermark (id, run_id, epoch, updated_at) VALUES (1, 'migration', 'migration', CURRENT_TIMESTAMP)")

    # Snapshot records predate the causal contract.  These fields are additive
    # and allow old snapshots to coexist while new snapshots identify the
    # committed epoch and complete watermark vector.
    op.add_column(
        "exchange_snapshot_record",
        sa.Column("schema_version", sa.String(16), nullable=False, server_default="1"),
    )
    op.add_column(
        "exchange_snapshot_record",
        sa.Column("epoch", sa.String(128), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "exchange_snapshot_record",
        sa.Column("watermarks_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "exchange_snapshot_record",
        sa.Column("committed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Journal mode is initialized and verified by alembic/env.py outside the
    # migration transaction, including databases already at this revision.


def downgrade() -> None:
    op.drop_column("exchange_snapshot_record", "committed")
    op.drop_column("exchange_snapshot_record", "watermarks_json")
    op.drop_column("exchange_snapshot_record", "epoch")
    op.drop_column("exchange_snapshot_record", "schema_version")
    op.drop_table("causal_watermark")
    op.drop_index("idx_causal_execution_state", table_name="causal_execution_bundle")
    op.drop_index("idx_causal_execution_sequence", table_name="causal_execution_bundle")
    op.drop_index("idx_causal_execution_command", table_name="causal_execution_bundle")
    op.drop_table("causal_execution_bundle")
    op.drop_index("idx_causal_command_client_order", table_name="causal_command_journal")
    op.drop_index("idx_causal_command_account_created", table_name="causal_command_journal")
    op.drop_index("idx_causal_command_status_updated", table_name="causal_command_journal")
    op.drop_table("causal_command_journal")
