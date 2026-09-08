from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PaperSession(Base):
    __tablename__ = "paper_sessions"
    __table_args__ = (Index("idx_paper_sessions_user_expiry", "user_id", "expires_at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255))


class PaperAccountRun(Base):
    __tablename__ = "paper_account_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "run_id", name="uq_paper_account_run_user_run"),
        Index("idx_paper_account_runs_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(96), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    account_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PaperAsset(Base):
    __tablename__ = "paper_assets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(255))
    display_precision: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class PaperBrandConfig(Base):
    __tablename__ = "paper_brand_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    exchange_name: Mapped[str] = mapped_column(String(96), nullable=False, default="Paper Exchange")
    logo_url: Mapped[str | None] = mapped_column(String(255))
    favicon_url: Mapped[str | None] = mapped_column(String(255))
    primary_color: Mapped[str] = mapped_column(String(32), nullable=False, default="#22d3ee")
    default_language: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    footer_text: Mapped[str] = mapped_column(String(255), nullable=False, default="Paper Trading / 模拟交易")
    paper_notice: Mapped[str] = mapped_column(String(255), nullable=False, default="所有资产均为模拟资金，不涉及真实资金。")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class PaperSystemSetting(Base):
    __tablename__ = "paper_system_settings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    value_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class PaperLiquidityConfig(Base):
    __tablename__ = "paper_liquidity_configs"
    __table_args__ = (UniqueConstraint("market_id", name="uq_paper_liquidity_market"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), nullable=False)
    levels_per_side: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    spread_bps: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("10"))
    level_spacing_bps: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False, default=Decimal("5"))
    depth_quote_per_side: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("5000000"))
    refresh_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    flow_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flow_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    flow_notional: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False, default=Decimal("20000"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_action: Mapped[str] = mapped_column(String(16), nullable=False, default="hold")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class PaperGlobalRun(Base):
    __tablename__ = "paper_global_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    run_id: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    global_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class PaperResetRecord(Base):
    __tablename__ = "paper_reset_records"
    __table_args__ = (Index("idx_paper_reset_records_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    old_run_id: Mapped[str | None] = mapped_column(String(96))
    new_run_id: Mapped[str | None] = mapped_column(String(96))
    old_epoch: Mapped[int | None] = mapped_column(Integer)
    new_epoch: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
