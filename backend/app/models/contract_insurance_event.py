from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractInsuranceEvent(Base):
    __tablename__ = "contract_insurance_events"
    __table_args__ = (
        Index("idx_contract_insurance_events_asset_time", "margin_asset", "created_at"),
        Index("idx_contract_insurance_events_market_time", "market_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    margin_asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    balance_before: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    residual_bad_debt: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    market_id: Mapped[int | None] = mapped_column(ForeignKey("markets.id"))
    related_liquidation_event_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
