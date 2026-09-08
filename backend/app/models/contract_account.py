from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractAccount(Base):
    __tablename__ = "contract_accounts"
    __table_args__ = (UniqueConstraint("user_id", "margin_asset", name="uq_contract_account_user_margin_asset"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    margin_asset: Mapped[str] = mapped_column(String(16), nullable=False, default="USDT")
    wallet_balance: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    available_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    used_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    total_fees: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
