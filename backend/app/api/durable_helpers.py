"""Durable-consistency helpers shared by the platform order APIs.

一体化平台契约（facts durable）下，用户订单走 slow durable 路径：写前先排空
write-behind 队列并等待合约仓位收敛，避免机器人报价的 fast-path 重放与用户
慢路径写入互相穿插导致状态回退。此前该逻辑只存在于 paper 端点，现上移为
通用 API 的默认行为。
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ZERO
from app.models.market import Market
from app.models.user import User


async def drain_writer(request: Request, timeout_seconds: float = 15.0) -> bool:
    """Wait for the write-behind pipeline to fully materialize before a slow-path write."""
    writer = getattr(request.app.state.runtime, "persistence_writer", None)
    if writer is None:
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            queue_empty = bool(writer._queue.empty())
            not_busy = not bool(getattr(writer, "_busy", False))
            lag = int(writer.materialization_lag())
        except (AttributeError, RuntimeError, TypeError):
            return True
        if queue_empty and not_busy and lag <= 0:
            return True
        await asyncio.sleep(0.05)
    try:
        return bool(writer._queue.empty()) and not bool(getattr(writer, "_busy", False)) and int(writer.materialization_lag()) <= 0
    except (AttributeError, RuntimeError, TypeError):
        return True


async def wait_contract_position_converged(
    request: Request,
    session: AsyncSession,
    user: User,
    market: Market,
    timeout_seconds: float = 10.0,
) -> bool:
    """Wait until the durable DB position has caught up with the fast mirror."""
    from app.db.session import SessionLocal

    service = request.app.state.contract_service
    user_id = int(user.id)
    market_id = int(market.id)
    mirror = request.app.state.runtime.clearinghouse.position_snapshot(user_id, market_id)
    mirror_qty = Decimal(mirror.quantity) if mirror is not None else ZERO
    if mirror_qty <= ZERO:
        return True
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        db_qty = ZERO
        try:
            async with SessionLocal() as probe:
                probe_market = await probe.get(Market, market_id)
                if probe_market is not None:
                    position = await service.get_position(probe, user_id, probe_market, create=False)
                    db_qty = Decimal(position.quantity or ZERO) if position is not None else ZERO
        except Exception:  # pragma: no cover - transient writer contention
            db_qty = ZERO
        if db_qty >= mirror_qty - Decimal("1e-9"):
            return True
        await asyncio.sleep(0.05)
    return False
