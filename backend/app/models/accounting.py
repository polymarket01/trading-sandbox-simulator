from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountingTransaction(Base):
    __tablename__ = "accounting_transactions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_accounting_transaction_idempotency"),
        UniqueConstraint("source_ledger_entry_id", name="uq_accounting_transaction_source_ledger"),
        Index("idx_accounting_transaction_source_trade", "source_trade_id"),
        Index("idx_accounting_transaction_source_event", "source_event_id"),
        Index("idx_accounting_transaction_effective", "effective_at"),
    )

    transaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    source_order_id: Mapped[str | None] = mapped_column(String(64))
    source_trade_id: Mapped[str | None] = mapped_column(String(64))
    source_event_id: Mapped[str | None] = mapped_column(String(64))
    source_batch_id: Mapped[str | None] = mapped_column(String(64))
    source_ledger_entry_id: Mapped[str | None] = mapped_column(String(64))
    reversal_of_transaction_id: Mapped[str | None] = mapped_column(ForeignKey("accounting_transactions.transaction_id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="committed")
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1")


class AccountingEntry(Base):
    __tablename__ = "accounting_entries"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_accounting_entry_nonnegative"),
        CheckConstraint("NOT (debit > 0 AND credit > 0)", name="ck_accounting_entry_one_side"),
        CheckConstraint("debit > 0 OR credit > 0", name="ck_accounting_entry_nonempty"),
        Index("idx_accounting_entry_transaction", "transaction_id"),
        Index("idx_accounting_entry_owner_asset", "account_domain", "owner_id", "asset"),
    )

    entry_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transaction_id: Mapped[str] = mapped_column(ForeignKey("accounting_transactions.transaction_id"), nullable=False)
    account_domain: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_type: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    account_code: Mapped[str] = mapped_column(String(64), nullable=False)
    debit: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    credit: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
