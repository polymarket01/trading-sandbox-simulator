from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractInsuranceFund(Base):
    __tablename__ = "contract_insurance_funds"
    __table_args__ = (UniqueConstraint("margin_asset", name="uq_contract_insurance_fund_margin_asset"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    margin_asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    balance: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
