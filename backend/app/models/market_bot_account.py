from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketBotAccount(Base):
    __tablename__ = "market_bot_accounts"
    __table_args__ = (
        UniqueConstraint("market_id", "user_id", name="uq_market_bot_market_user"),
        UniqueConstraint("market_id", "bot_label", name="uq_market_bot_market_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    bot_label: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="maker")
    strategy_role: Mapped[str | None] = mapped_column(String(32))
    initial_quote_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    initial_base_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    initial_base_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
