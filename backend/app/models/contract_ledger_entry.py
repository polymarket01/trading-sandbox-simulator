from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractLedgerEntry(Base):
    __tablename__ = "contract_ledger_entries"
    __table_args__ = (
        Index("idx_contract_ledger_user_asset_time", "user_id", "margin_asset", "created_at"),
        Index("idx_contract_ledger_market_time", "market_id", "created_at"),
        Index("idx_contract_ledger_related_order", "related_order_id"),
        Index("idx_contract_ledger_related_trade", "related_trade_id"),
        Index("idx_contract_ledger_related_event", "related_event_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"))
    margin_asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    wallet_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    wallet_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    available_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    available_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    used_margin_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    used_margin_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    unrealized_pnl_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    unrealized_pnl_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    total_fees_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    total_fees_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    related_order_id: Mapped[str | None] = mapped_column(String(64))
    related_trade_id: Mapped[str | None] = mapped_column(String(64))
    related_event_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
