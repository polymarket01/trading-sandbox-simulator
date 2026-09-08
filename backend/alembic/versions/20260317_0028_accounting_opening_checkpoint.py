from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0028"
down_revision = "20260317_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounting_transactions", sa.Column("source_ledger_entry_id", sa.String(64), nullable=True))
    # Historical committed transactions remain byte-for-byte immutable. Their
    # legacy ledger id stays in metadata and is absorbed by the explicit
    # opening checkpoint. Only post-0028 transactions use this structured key.
    op.create_index(
        "uq_accounting_transaction_source_ledger",
        "accounting_transactions",
        ["source_ledger_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    source_count = int(
        op.get_bind().execute(
            sa.text("SELECT count(*) FROM accounting_transactions WHERE source_ledger_entry_id IS NOT NULL")
        ).scalar_one()
        or 0
    )
    if source_count:
        raise RuntimeError(
            "unsafe downgrade refused: structured accounting provenance exists; "
            "restore the pre-0028 backup instead"
        )
    op.drop_index("uq_accounting_transaction_source_ledger", table_name="accounting_transactions")
    with op.batch_alter_table("accounting_transactions") as batch:
        batch.drop_column("source_ledger_entry_id")
