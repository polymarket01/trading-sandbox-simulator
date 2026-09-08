from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractAdlEvent(Base):
    __tablename__ = "contract_adl_events"
    __table_args__ = (
        Index("idx_contract_adl_events_liquidation", "liquidation_event_id", "created_at"),
        Index("idx_contract_adl_events_market_time", "market_id", "created_at"),
        Index("idx_contract_adl_events_user_time", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    liquidation_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    execution_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    released_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    bad_debt_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    covered_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    residual_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    pnl_pct: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    effective_leverage: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    rank_score: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="executed")
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="insurance_shortfall_adl")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
