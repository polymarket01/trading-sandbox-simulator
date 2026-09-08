from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MARKET_VISIBILITY_LISTED = "listed"
MARKET_VISIBILITY_TEST = "test"


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    product_type: Mapped[str] = mapped_column(String(16), nullable=False, default="SPOT")
    market_type: Mapped[str] = mapped_column(String(32), nullable=False, default="listed")
    # 一体化平台的市场可见性：listed = 对外白标市场（用户可交易，走 PRD
    # 上币状态机）；test = 策略实验市场（仅 ops 可见/可交易，可随意 wipe）。
    visibility: Mapped[str | None] = mapped_column(String(16))
    base_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    quote_asset: Mapped[str] = mapped_column(String(16), nullable=False)
    margin_asset: Mapped[str | None] = mapped_column(String(16))
    price_tick: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    qty_step: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    min_qty: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    min_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    max_leverage: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)
    default_leverage: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=1)
    maintenance_margin_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    funding_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    funding_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    index_price_source: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    mark_price_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="orderbook")
    funding_rate_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="binance")
    funding_interest_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.0001"))
    funding_clamp_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.0005"))
    funding_cap_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.02"))
    funding_impact_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("25000"))
    contract_trading_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="normal")
    paper_status: Mapped[str] = mapped_column(String(32), nullable=False, default="TRADING", server_default="TRADING")
    price_source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    price_source_symbol: Mapped[str | None] = mapped_column(String(64))
    price_source_stale_after_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default="30")
    price_protection_pct: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("0.05"), server_default="0.05")
    delist_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reference_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    price_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    default_maker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    default_taker_fee_rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def is_listed(self) -> bool:
        """是否对外白标市场（listed）。

        visibility 未迁移的历史行按 paper_status 推断：处于非默认 TRADING
        状态（PAUSED/REVIEW 等）说明它是产品市场；沙盒市场恒为 TRADING。
        """
        if self.visibility is not None:
            return str(self.visibility) == MARKET_VISIBILITY_LISTED
        return str(self.paper_status or "TRADING") != "TRADING"
