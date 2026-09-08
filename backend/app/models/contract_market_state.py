from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractMarketState(Base):
    __tablename__ = "contract_market_states"
    __table_args__ = (UniqueConstraint("market_id", name="uq_contract_market_state_market"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    index_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    index_source: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    index_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    external_mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_source: Mapped[str] = mapped_column(String(32), nullable=False, default="orderbook")
    local_mid_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    local_last_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    impact_bid_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    impact_ask_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    premium_index: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    funding_rate_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.0001"))
    funding_clamp_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.0005"))
    funding_cap_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.02"))
    impact_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("25000"))
    next_funding_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_status: Mapped[str] = mapped_column(String(32), nullable=False, default="fallback")
    source_message: Mapped[str | None] = mapped_column(String(512))
    external_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
