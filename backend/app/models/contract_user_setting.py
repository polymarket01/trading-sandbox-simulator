from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractUserSetting(Base):
    __tablename__ = "contract_user_settings"
    __table_args__ = (UniqueConstraint("user_id", "market_id", name="uq_contract_user_setting_user_market"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    leverage: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="isolated")
    position_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="one_way")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
