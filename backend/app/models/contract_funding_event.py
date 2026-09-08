from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractFundingEvent(Base):
    __tablename__ = "contract_funding_events"
    __table_args__ = (Index("idx_contract_funding_events_market_time", "market_id", "funding_time"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    position_side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    index_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    funding_rate_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="formula")
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
