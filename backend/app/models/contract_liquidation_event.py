from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractLiquidationEvent(Base):
    __tablename__ = "contract_liquidation_events"
    __table_args__ = (Index("idx_contract_liquidation_events_market_time", "market_id", "liquidated_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    liquidation_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    bankruptcy_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    released_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    insurance_covered: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    residual_bad_debt: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    adl_covered: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    adl_residual: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    adl_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_required")
    maintenance_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    margin_buffer: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    risk_status: Mapped[str] = mapped_column(String(32), nullable=False, default="liquidation_due")
    reason: Mapped[str] = mapped_column(String(64), nullable=False, default="mark_price_liquidation")
    liquidated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
