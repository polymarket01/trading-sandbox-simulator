from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("idx_orders_user_market", "user_id", "market_id", "created_at"),
        Index("idx_orders_market_status", "market_id", "status", "created_at"),
        Index("idx_orders_market_status_sequence", "market_id", "status", "sequence_number"),
        Index("idx_orders_status_market_user", "status", "market_id", "user_id"),
        Index("idx_orders_user_market_client_status", "user_id", "market_id", "client_order_id", "status", "created_at"),
        Index("idx_orders_product_market_time", "product_type", "market_id", "created_at"),
        Index("idx_orders_product_time", "product_type", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    client_order_id: Mapped[str | None] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_run_id: Mapped[str | None] = mapped_column(String(96))
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SPOT")
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    position_action: Mapped[str | None] = mapped_column(String(16))
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    leverage: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    tif: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=0)
    avg_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    notional: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    remaining_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    protection_bps: Mapped[int | None] = mapped_column(Integer)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    reject_reason: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
