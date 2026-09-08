from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0027"
down_revision = "20260317_0026"
branch_labels = None
depends_on = None


def dec() -> sa.Numeric:
    return sa.Numeric(36, 18)


def upgrade() -> None:
    with op.batch_alter_table("trades") as batch:
        batch.add_column(sa.Column("business_key", sa.String(length=191), nullable=True))
        batch.create_unique_constraint("uq_trades_business_key", ["business_key"])
    op.execute("UPDATE trades SET business_key = 'legacy:' || trade_id WHERE business_key IS NULL")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_live_client_key "
        "ON orders (user_id, market_id, product_type, client_order_id) "
        "WHERE client_order_id IS NOT NULL AND status IN ('new', 'partially_filled')"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_funding_event_user_market_time "
        "ON contract_funding_events (user_id, market_id, funding_time)"
    )
    op.create_index("idx_ledger_related_order", "ledger_entries", ["related_order_id"])
    op.create_index("idx_ledger_related_trade", "ledger_entries", ["related_trade_id"])
    op.create_index("idx_contract_ledger_related_order", "contract_ledger_entries", ["related_order_id"])
    op.create_index("idx_contract_ledger_related_trade", "contract_ledger_entries", ["related_trade_id"])
    op.create_index("idx_contract_ledger_related_event", "contract_ledger_entries", ["related_event_id"])
    op.create_index("idx_trades_taker_order", "trades", ["taker_order_id"])
    op.create_index("idx_trades_maker_order", "trades", ["maker_order_id"])
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False, unique=True),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watermark_json", sa.JSON(), nullable=False),
        sa.Column("summary_json", sa.JSON(), nullable=False),
        sa.Column("difference_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("blocking_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("allow_trading", sa.String(16), nullable=False, server_default="unknown"),
    )
    op.create_index("idx_reconciliation_runs_started", "reconciliation_runs", ["started_at"])
    op.create_table(
        "reconciliation_differences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("difference_id", sa.String(64), nullable=False, unique=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("check_code", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("entity_type", sa.String(32)),
        sa.Column("entity_id", sa.String(128)),
        sa.Column("user_id", sa.Integer()),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_reconciliation_diff_run_check", "reconciliation_differences", ["run_id", "check_code"])
    op.create_index("idx_reconciliation_diff_domain", "reconciliation_differences", ["domain", "severity"])
    op.create_table(
        "accounting_transactions",
        sa.Column("transaction_id", sa.String(64), primary_key=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(191), nullable=False, unique=True),
        sa.Column("source_order_id", sa.String(64)),
        sa.Column("source_trade_id", sa.String(64)),
        sa.Column("source_event_id", sa.String(64)),
        sa.Column("source_batch_id", sa.String(64)),
        sa.Column("reversal_of_transaction_id", sa.String(64), sa.ForeignKey("accounting_transactions.transaction_id")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(16), nullable=False),
    )
    op.create_index("idx_accounting_transaction_source_trade", "accounting_transactions", ["source_trade_id"])
    op.create_index("idx_accounting_transaction_source_event", "accounting_transactions", ["source_event_id"])
    op.create_index("idx_accounting_transaction_effective", "accounting_transactions", ["effective_at"])
    op.create_table(
        "accounting_entries",
        sa.Column("entry_id", sa.String(64), primary_key=True),
        sa.Column("transaction_id", sa.String(64), sa.ForeignKey("accounting_transactions.transaction_id"), nullable=False),
        sa.Column("account_domain", sa.String(32), nullable=False),
        sa.Column("owner_type", sa.String(32), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("account_code", sa.String(64), nullable=False),
        sa.Column("debit", dec(), nullable=False, server_default="0"),
        sa.Column("credit", dec(), nullable=False, server_default="0"),
        sa.Column("quantity", dec(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("debit >= 0 AND credit >= 0", name="ck_accounting_entry_nonnegative"),
        sa.CheckConstraint("NOT (debit > 0 AND credit > 0)", name="ck_accounting_entry_one_side"),
        sa.CheckConstraint("debit > 0 OR credit > 0", name="ck_accounting_entry_nonempty"),
    )
    op.create_index("idx_accounting_entry_transaction", "accounting_entries", ["transaction_id"])
    op.create_index("idx_accounting_entry_owner_asset", "accounting_entries", ["account_domain", "owner_id", "asset"])
    op.execute("CREATE TRIGGER accounting_transactions_committed_no_update BEFORE UPDATE ON accounting_transactions WHEN OLD.status = 'committed' BEGIN SELECT RAISE(ABORT, 'committed accounting transaction is immutable'); END")
    op.execute("CREATE TRIGGER accounting_transactions_committed_no_delete BEFORE DELETE ON accounting_transactions WHEN OLD.status = 'committed' BEGIN SELECT RAISE(ABORT, 'committed accounting transaction is immutable'); END")
    op.execute("CREATE TRIGGER accounting_entries_no_update BEFORE UPDATE ON accounting_entries BEGIN SELECT RAISE(ABORT, 'accounting entry is immutable'); END")
    op.execute("CREATE TRIGGER accounting_entries_no_delete BEFORE DELETE ON accounting_entries BEGIN SELECT RAISE(ABORT, 'accounting entry is immutable'); END")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS accounting_entries_no_delete")
    op.execute("DROP TRIGGER IF EXISTS accounting_entries_no_update")
    op.execute("DROP TRIGGER IF EXISTS accounting_transactions_committed_no_delete")
    op.execute("DROP TRIGGER IF EXISTS accounting_transactions_committed_no_update")
    op.drop_table("accounting_entries")
    op.drop_table("accounting_transactions")
    op.drop_table("reconciliation_differences")
    op.drop_table("reconciliation_runs")
    op.execute("DROP INDEX IF EXISTS uq_funding_event_user_market_time")
    op.execute("DROP INDEX IF EXISTS uq_orders_live_client_key")
    op.drop_index("idx_trades_maker_order", table_name="trades")
    op.drop_index("idx_trades_taker_order", table_name="trades")
    op.drop_index("idx_contract_ledger_related_event", table_name="contract_ledger_entries")
    op.drop_index("idx_contract_ledger_related_trade", table_name="contract_ledger_entries")
    op.drop_index("idx_contract_ledger_related_order", table_name="contract_ledger_entries")
    op.drop_index("idx_ledger_related_trade", table_name="ledger_entries")
    op.drop_index("idx_ledger_related_order", table_name="ledger_entries")
    with op.batch_alter_table("trades") as batch:
        batch.drop_constraint("uq_trades_business_key", type_="unique")
        batch.drop_column("business_key")
