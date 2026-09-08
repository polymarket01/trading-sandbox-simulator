from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.core.config import settings
from app.core.security import hash_session_token
from app.models.paper_exchange import PaperSession
from app.models.user import User


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_api_key: str | None = Header(default=None),
) -> User:
    if x_api_key:
        result = await session.execute(select(User).where(User.api_key == x_api_key, User.is_active.is_(True)))
        user = result.scalar_one_or_none()
        if user is not None:
            return user
    token = request.cookies.get(settings.paper_exchange_cookie_name)
    if token:
        now = datetime.now(tz=UTC)
        row = await session.scalar(
            select(PaperSession).where(
                PaperSession.token_hash == hash_session_token(token),
                PaperSession.expires_at > now,
            )
        )
        if row is not None:
            user = await session.scalar(select(User).where(User.id == row.user_id, User.is_active.is_(True)))
            if user is not None:
                row.last_seen_at = now
                return user
    if not x_api_key and not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
