from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FeeProfile(Base):
    __tablename__ = "fee_profiles"
    __table_args__ = (UniqueConstraint("user_id", "market_id", name="uq_fee_profile_user_market"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"))
    maker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    taker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
