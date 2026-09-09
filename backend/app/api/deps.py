from __future__ import annotations

from datetime import UTC, datetime
import ipaddress

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.core.config import settings
from app.core.security import hash_session_token
from app.models.paper_exchange import PaperSession
from app.models.user import User


def bot_request_allowed(request) -> bool:
    if settings.external_bot_api_enabled:
        return True
    if request.headers.get("x-forwarded-for"):
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        return False


def check_user_boundary(request, user):
    if user.role == "mm_bot" and not bot_request_allowed(request):
        raise HTTPException(403, "internal bot API is not public")
    return user


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_api_key: str | None = Header(default=None),
) -> User:
    if x_api_key:
        result = await session.execute(select(User).where(User.api_key == x_api_key, User.is_active.is_(True)))
        user = result.scalar_one_or_none()
        if user is not None:
            return check_user_boundary(request, user)
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
                # Authentication reads must not acquire a SQLite writer lock.
                # Session expiry is fixed at login, independent of last_seen_at.
                return check_user_boundary(request, user)
    if not x_api_key and not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要登录")
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效")


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return user
