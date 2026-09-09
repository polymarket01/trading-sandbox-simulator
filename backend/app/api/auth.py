from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.db.session import get_db_session
from app.models.market import Market
from app.models.paper_exchange import PaperSession
from app.models.user import User
from app.schemas.api import LoginRequest, PasswordChangeRequest, RegisterRequest
from app.services.paper_account_service import (
    create_session,
    delete_session,
    normalize_username,
    publish_new_user_runtime_state,
    register_user,
    serialize_user,
)
from app.services.persistence_contract import paper_product_enabled


router = APIRouter(tags=["auth"])


def serialize_login_user(user: User) -> dict:
    return serialize_user(user)


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    normalized = normalize_username(payload.username)
    user = await session.scalar(
        select(User).where(
            (User.username_normalized == normalized) | (User.username == payload.username.strip()),
            User.is_active.is_(True),
        )
    )
    if user is None or user.role == "mm_bot" or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if paper_product_enabled():
        raw_token = await create_session(
            session,
            user,
            user_agent=request.headers.get("user-agent"),
            remember=payload.remember,
        )
        response.set_cookie(
            settings.paper_exchange_cookie_name,
            raw_token,
            max_age=int(settings.paper_exchange_session_ttl_seconds if payload.remember else 86400),
            httponly=True,
            secure=bool(settings.paper_exchange_cookie_secure),
            samesite="lax",
            path=(settings.public_base_path.rstrip("/") + "/"),
        )
    return {"user": serialize_login_user(user), "paper_mode": paper_product_enabled()}


@router.post("/auth/register", status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    if not paper_product_enabled() or not settings.paper_exchange_register_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="注册功能未启用")
    markets = list(
        (await session.execute(select(Market).where(Market.is_active.is_(True)))).scalars()
    )
    try:
        user = await register_user(session, payload.username, payload.password, markets)
        await publish_new_user_runtime_state(session, user, request.app.state.runtime)
        raw_token = await create_session(
            session,
            user,
            user_agent=request.headers.get("user-agent"),
            remember=True,
        )
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    response.set_cookie(
        settings.paper_exchange_cookie_name,
        raw_token,
        max_age=int(settings.paper_exchange_session_ttl_seconds),
        httponly=True,
        secure=bool(settings.paper_exchange_cookie_secure),
        samesite="lax",
        path=(settings.public_base_path.rstrip("/") + "/"),
    )
    return {"user": serialize_login_user(user), "paper_mode": True}


@router.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {"user": serialize_login_user(user), "paper_mode": paper_product_enabled()}


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
):
    await delete_session(session, request.cookies.get(settings.paper_exchange_cookie_name))
    response.delete_cookie(settings.paper_exchange_cookie_name, path=(settings.public_base_path.rstrip("/") + "/"))
    return {"ok": True}


@router.post("/auth/change-password")
async def change_password(
    payload: PasswordChangeRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码错误")
    user.password_hash = hash_password(payload.new_password)
    await session.commit()
    return {"ok": True}


@router.get("/auth/api-key")
async def own_api_key(response: Response, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    if user.role == "mm_bot":
        raise HTTPException(403, "机器人身份不提供对外凭据")
    from app.core.security import generate_api_key, generate_api_secret
    if not user.api_key:
        user.api_key = generate_api_key("user")
    if not user.api_secret_hash:
        user.api_secret_hash = generate_api_secret()
    await session.flush()
    response.headers["Cache-Control"] = "no-store"
    return {"username": user.username, "api_key": user.api_key, "api_secret": user.api_secret_hash,
            "authentication": "X-API-Key", "scope": "own_account", "is_simulated": True}
