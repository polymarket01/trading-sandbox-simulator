from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    price_tick: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    qty_step: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    min_qty: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    price_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_maker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    default_taker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
