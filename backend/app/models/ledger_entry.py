from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (Index("idx_ledger_user_asset_time", "user_id", "asset", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    asset: Mapped[str] = mapped_column(String(16), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    balance_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    available_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    available_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    frozen_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    frozen_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    related_order_id: Mapped[str | None] = mapped_column(String(64))
    related_trade_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
