from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContractPosition(Base):
    __tablename__ = "contract_positions"
    __table_args__ = (
        UniqueConstraint("user_id", "market_id", "side", name="uq_contract_position_user_market_side"),
        Index("idx_contract_positions_market", "market_id", "side"),
    )

    @classmethod
    def active_filters(cls):
        """Current exposure only; flat rows retain cumulative realized PnL."""
        return cls.side.in_(("long", "short")), cls.quantity > 0

    @property
    def is_active(self) -> bool:
        return self.side in ("long", "short") and Decimal(self.quantity or 0) > 0

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False, default="flat")
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    mark_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    liquidation_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    leverage: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)
    margin_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="isolated")
    isolated_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    maintenance_margin: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
