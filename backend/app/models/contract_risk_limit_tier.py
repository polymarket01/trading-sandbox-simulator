from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractRiskLimitTier(Base):
    __tablename__ = "contract_risk_limit_tiers"
    __table_args__ = (
        UniqueConstraint("market_id", "tier", name="uq_contract_risk_tier_market_tier"),
        Index("idx_contract_risk_tiers_market_floor", "market_id", "notional_floor"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    notional_floor: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    notional_cap: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    max_leverage: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)
    maintenance_margin_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    maintenance_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
