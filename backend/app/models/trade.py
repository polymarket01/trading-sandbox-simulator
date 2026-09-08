from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_market_time", "market_id", "executed_at"),
        Index("idx_trades_user_time", "taker_user_id", "executed_at"),
        Index("idx_trades_product_market_time", "product_type", "market_id", "executed_at"),
        Index("idx_trades_product_time", "product_type", "executed_at"),
        Index("idx_trades_taker_order", "taker_order_id"),
        Index("idx_trades_maker_order", "maker_order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    trade_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    business_key: Mapped[str | None] = mapped_column(String(191), unique=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SPOT")
    price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    quote_amount: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    taker_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    maker_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    taker_position_action: Mapped[str | None] = mapped_column(String(16))
    maker_position_action: Mapped[str | None] = mapped_column(String(16))
    taker_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    maker_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    taker_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    maker_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    taker_account_run_id: Mapped[str | None] = mapped_column(String(96))
    maker_account_run_id: Mapped[str | None] = mapped_column(String(96))
    global_run_id: Mapped[str | None] = mapped_column(String(96))
    taker_side: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    maker_fee: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    taker_fee: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    fee_asset_maker: Mapped[str | None] = mapped_column(String(16))
    fee_asset_taker: Mapped[str | None] = mapped_column(String(16))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
