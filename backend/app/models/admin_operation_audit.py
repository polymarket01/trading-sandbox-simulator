from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdminOperationAudit(Base):
    __tablename__ = "admin_operation_audits"
    __table_args__ = (
        Index("idx_admin_ops_created", "created_at"),
        Index("idx_admin_ops_domain_created", "domain", "created_at"),
        Index("idx_admin_ops_type_created", "operation_type", "created_at"),
        Index("idx_admin_ops_symbol_created", "target_symbol", "created_at"),
        Index("idx_admin_ops_actor_created", "actor_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_username: Mapped[str | None] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128))
    target_symbol: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    summary: Mapped[str] = mapped_column(String(255), nullable=False)
    result_json: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
