from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractFundingSettlement(Base):
    __tablename__ = "contract_funding_settlements"
    __table_args__ = (
        UniqueConstraint("market_id", "funding_time", name="uq_contract_funding_settlement_market_time"),
        Index("idx_contract_funding_settlements_market_time", "market_id", "funding_time"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    settlement_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    funding_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    funding_rate_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="formula")
    index_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    settled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="settling")
    error_message: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
