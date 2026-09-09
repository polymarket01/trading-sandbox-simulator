from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import public
from app.api import admin as admin_api
from app.api.durable_helpers import drain_writer, wait_contract_position_converged
from app.api.deps import get_admin_user, get_current_user
from app.core.config import settings
from app.core.constants import (
    PAPER_MARKET_CANCEL_ONLY,
    PAPER_MARKET_DELISTED,
    PAPER_MARKET_DELISTING,
    PAPER_MARKET_DRAFT,
    PAPER_MARKET_PAUSED,
    PAPER_MARKET_PRE_OPEN,
    PAPER_MARKET_REDUCE_ONLY,
    PAPER_MARKET_TRADING,
    PAPER_MARKET_VALIDATING,
    PRODUCT_TYPE_PERP,
    PRODUCT_TYPE_SPOT,
    ZERO,
)
from app.core.decimal_utils import decimal_to_str, quantize_scale
from app.core.time_utils import to_millis
from app.db.session import get_db_session
from app.models.balance import Balance
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_position import ContractPosition
from app.models.market import MARKET_VISIBILITY_LISTED, Market
from app.models.market_bot_account import MarketBotAccount
from app.models.contract_user_setting import ContractUserSetting
from app.models.paper_exchange import (
    PaperAsset,
    PaperBrandConfig,
    PaperGlobalRun,
    PaperLiquidityConfig,
    PaperResetRecord,
    PaperSession,
    PaperSystemSetting,
    PaperAccountRun,
)
from app.models.trade import Trade
from app.models.order import Order
from app.models.user import User
from app.schemas.api import (
    ContractLeverageUpdateRequest,
    ContractOrderAmendRequest,
    ContractOrderCreateRequest,
    OrderAmendRequest,
    OrderCreateRequest,
)
from app.services.contract_service import ContractService, ContractValidationError
from app.services.order_service import OrderService, OrderValidationError
from app.services.paper_account_service import (
    ensure_user_assets,
    paper_maker_inventory_target,
    reset_all_accounts,
    reset_user_account,
    serialize_user,
)
from app.services.persistence_contract import paper_product_enabled, public_persistence_contract


router = APIRouter(prefix="/paper", tags=["paper-exchange"])

PAPER_STATUSES = {
    PAPER_MARKET_DRAFT,
    PAPER_MARKET_VALIDATING,
    PAPER_MARKET_PRE_OPEN,
    PAPER_MARKET_TRADING,
    PAPER_MARKET_PAUSED,
    PAPER_MARKET_CANCEL_ONLY,
    PAPER_MARKET_REDUCE_ONLY,
    PAPER_MARKET_DELISTING,
    PAPER_MARKET_DELISTED,
}
STATUS_TRANSITIONS = {
    PAPER_MARKET_DRAFT: {PAPER_MARKET_VALIDATING, PAPER_MARKET_DELISTED},
    PAPER_MARKET_VALIDATING: {PAPER_MARKET_PRE_OPEN, PAPER_MARKET_PAUSED, PAPER_MARKET_DRAFT},
    PAPER_MARKET_PRE_OPEN: {PAPER_MARKET_TRADING, PAPER_MARKET_PAUSED, PAPER_MARKET_DRAFT},
    PAPER_MARKET_TRADING: {PAPER_MARKET_PAUSED, PAPER_MARKET_CANCEL_ONLY, PAPER_MARKET_REDUCE_ONLY, PAPER_MARKET_DELISTING},
    PAPER_MARKET_PAUSED: {PAPER_MARKET_PRE_OPEN, PAPER_MARKET_TRADING, PAPER_MARKET_CANCEL_ONLY, PAPER_MARKET_DELISTING},
    PAPER_MARKET_CANCEL_ONLY: {PAPER_MARKET_TRADING, PAPER_MARKET_PAUSED, PAPER_MARKET_DELISTING},
    PAPER_MARKET_REDUCE_ONLY: {PAPER_MARKET_TRADING, PAPER_MARKET_PAUSED, PAPER_MARKET_DELISTING},
    PAPER_MARKET_DELISTING: {PAPER_MARKET_DELISTED},
    PAPER_MARKET_DELISTED: set(),
}


class PaperMarketCreateRequest(BaseModel):
    symbol: str = Field(min_length=3, max_length=32)
    product_type: Literal["SPOT", "PERP"] = "SPOT"
    base_asset: str = Field(min_length=1, max_length=16)
    quote_asset: str = "USDT"
    margin_asset: str | None = "USDT"
    reference_price: Decimal = Field(default=Decimal("1"), gt=0)
    price_tick: Decimal = Field(default=Decimal("0.01"), gt=0)
    qty_step: Decimal = Field(default=Decimal("0.001"), gt=0)
    min_qty: Decimal = Field(default=Decimal("0.001"), gt=0)
    min_notional: Decimal = Field(default=Decimal("5"), gt=0)
    max_leverage: Decimal = Field(default=Decimal("20"), gt=0)
    default_leverage: Decimal = Field(default=Decimal("5"), gt=0)


class PaperMarketUpdateRequest(BaseModel):
    status: str | None = None
    price_source: Literal["manual", "simulated", "binance"] | None = None
    reference_price: Decimal | None = Field(default=None, gt=0)
    price_source_symbol: str | None = None
    price_protection_pct: Decimal | None = Field(default=None, ge=0, le=1)
    enabled: bool | None = None


class PaperBrandUpdateRequest(BaseModel):
    exchange_name: str | None = Field(default=None, min_length=1, max_length=96)
    logo_url: str | None = None
    favicon_url: str | None = None
    primary_color: str | None = None
    default_language: str | None = None
    footer_text: str | None = None
    paper_notice: str | None = None


class PaperLiquidityUpdateRequest(BaseModel):
    levels_per_side: int | None = Field(default=None, ge=1, le=50)
    spread_bps: Decimal | None = Field(default=None, gt=0, le=10000)
    level_spacing_bps: Decimal | None = Field(default=None, ge=0, le=10000)
    depth_quote_per_side: Decimal | None = Field(default=None, gt=0)
    refresh_interval_ms: int | None = Field(default=None, ge=100, le=60000)
    flow_enabled: bool | None = None
    flow_interval_seconds: int | None = Field(default=None, ge=1, le=86400)
    flow_notional: Decimal | None = Field(default=None, gt=0)
    enabled: bool | None = None
    stale_action: Literal["hold", "cancel", "pause"] | None = None


class PaperResetRequest(BaseModel):
    reason: str = Field(default="user_reset", min_length=1, max_length=255)


class PaperAssetUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    icon: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=255)
    display_precision: int | None = Field(default=None, ge=0, le=18)
    status: Literal["ACTIVE", "INACTIVE"] | None = None


class PaperDefaultsUpdateRequest(BaseModel):
    spot_initial_usdt: Decimal | None = Field(default=None, gt=0)
    perp_initial_usdt: Decimal | None = Field(default=None, gt=0)
    default_leverage: Decimal | None = Field(default=None, gt=0)
    user_reset_enabled: bool | None = None


def _require_paper_mode() -> None:
    if not paper_product_enabled():
        raise HTTPException(status_code=404, detail="PaperTrading 模式未启用")


def _error(exc: Exception, code: str = "ORDER_REJECTED") -> HTTPException:
    return HTTPException(status_code=400, detail={"code": code, "message": str(exc)})


async def _retry_engine_miss(
    request: Request,
    session: AsyncSession,
    market: Market,
    call,
    *,
    error_text: str,
) -> dict:
    """Retry a cancel/amend once after rebuilding the engine book from the DB.

    A slow-path user order commits to the DB first; a concurrent quote-cycle
    engine rebuild can briefly drop that order from the in-memory book, which
    makes the engine report "order not found on book".  Rebuilding the book
    from the durable DB restores the order, then the operation retries once.
    """
    try:
        return await call()
    except (OrderValidationError, ContractValidationError) as exc:
        if error_text not in str(exc):
            raise
        await session.rollback()
        if market.product_type == PRODUCT_TYPE_SPOT:
            await request.app.state.order_service._rebuild_engine_book_from_db(
                session, int(market.id), reason="paper_engine_miss_retry"
            )
        else:
            await request.app.state.contract_service.rebuild_engine_book_from_db(
                session, int(market.id), reason="paper_engine_miss_retry"
            )
        return await call()


async def _place_paper_contract_close(
    request: Request,
    session: AsyncSession,
    user: User,
    payload: ContractOrderCreateRequest,
) -> dict:
    """Durable close with replay-lag protection.

    User closes are low-frequency and correctness-first: drain the writer so
    prior user events materialize before this write, then wait until the
    durable DB position has caught up with the fast mirror, then place through
    the slow path.  The convergence wait is user+market scoped so a startup
    backlog of robot quote tasks cannot break a user close; it can only delay
    it briefly.
    """
    service: ContractService = request.app.state.contract_service
    market = await session.scalar(select(Market).where(Market.symbol == str(payload.symbol).upper()))
    if market is None:
        raise _error(ValueError("market not found"), "MARKET_NOT_FOUND")
    await drain_writer(request)
    try:
        await wait_contract_position_converged(request, session, user, market, timeout_seconds=10.0)
    except ContractValidationError:
        await session.rollback()
    return await service.place_order(session, user, payload)


def _paper_decimal(value: object, scale: int = 8) -> str | None:
    if value is None:
        return None
    try:
        return decimal_to_str(quantize_scale(Decimal(str(value)), scale))
    except (TypeError, ValueError, ArithmeticError):
        return decimal_to_str(Decimal(str(value)))


def _clean_account_payload(payload: dict) -> dict:
    cleaned = dict(payload)
    for key in ("wallet_balance", "available_margin", "used_margin", "unrealized_pnl", "realized_pnl", "total_fees"):
        if key in cleaned:
            cleaned[key] = _paper_decimal(cleaned[key])
    return cleaned


async def _paper_markets(session: AsyncSession, *, include_non_trading: bool = False) -> list[Market]:
    stmt = select(Market).where(Market.visibility == MARKET_VISIBILITY_LISTED).order_by(Market.symbol.asc())
    if not include_non_trading:
        stmt = stmt.where(
            Market.is_active.is_(True),
            Market.paper_status.not_in({PAPER_MARKET_DRAFT, PAPER_MARKET_VALIDATING, PAPER_MARKET_DELISTED}),
        )
    return list((await session.execute(stmt)).scalars())


def _market_item(market: Market) -> dict:
    return {
        "id": int(market.id),
        "symbol": market.symbol,
        "product_type": market.product_type,
        "market_type": market.market_type,
        "base_asset": market.base_asset,
        "quote_asset": market.quote_asset,
        "margin_asset": market.margin_asset,
        "status": market.paper_status,
        "paper_status": market.paper_status,
        "price_source": market.price_source,
        "price_source_symbol": market.price_source_symbol,
        "reference_price": _paper_decimal(market.reference_price, max(8, int(market.price_precision or 0))),
        "price_tick": _paper_decimal(market.price_tick, max(8, int(market.price_precision or 0))),
        "qty_step": _paper_decimal(market.qty_step, max(8, int(market.qty_precision or 0))),
        "min_qty": _paper_decimal(market.min_qty, max(8, int(market.qty_precision or 0))),
        "min_notional": _paper_decimal(market.min_notional),
        "max_leverage": _paper_decimal(market.max_leverage),
        "default_leverage": _paper_decimal(market.default_leverage),
        "maintenance_margin_rate": _paper_decimal(market.maintenance_margin_rate),
        "funding_rate": _paper_decimal(market.funding_rate),
        "funding_interval_hours": int(market.funding_interval_hours),
        "price_protection_pct": _paper_decimal(market.price_protection_pct),
        "maker_fee_rate": _paper_decimal(market.default_maker_fee_rate),
        "taker_fee_rate": _paper_decimal(market.default_taker_fee_rate),
        "price_precision": int(market.price_precision),
        "qty_precision": int(market.qty_precision),
        "is_active": bool(market.is_active),
    }


async def _validate_paper_market(request: Request, session: AsyncSession, market: Market) -> dict:
    """Run deterministic pre-open checks without placing a customer order."""
    checks: list[dict[str, object]] = []

    def add(name: str, ok: bool, reason: str) -> None:
        checks.append({"name": name, "status": "passed" if ok else "failed", "reason": reason})

    add("symbol_unique", True, "symbol is already persisted uniquely")
    add(
        "market_rules",
        Decimal(market.price_tick or ZERO) > ZERO
        and Decimal(market.qty_step or ZERO) > ZERO
        and Decimal(market.min_qty or ZERO) > ZERO
        and Decimal(market.min_notional or ZERO) > ZERO,
        "tick, lot, minimum quantity and minimum notional must be positive",
    )
    if market.product_type == PRODUCT_TYPE_PERP:
        add(
            "leverage_rules",
            Decimal(market.default_leverage or ZERO) > ZERO
            and Decimal(market.max_leverage or ZERO) >= Decimal(market.default_leverage or ZERO),
            "default leverage must not exceed market maximum",
        )

    asset_codes = {str(market.base_asset).upper(), str(market.quote_asset).upper()}
    if market.margin_asset:
        asset_codes.add(str(market.margin_asset).upper())
    asset_rows = await session.execute(select(PaperAsset).where(PaperAsset.code.in_(asset_codes)))
    active_assets = {str(row.code).upper() for row in asset_rows.scalars() if str(row.status).upper() == "ACTIVE"}
    add(
        "assets_ready",
        active_assets == asset_codes,
        "all base, quote and margin assets must be ACTIVE",
    )

    maker = await session.scalar(
        select(MarketBotAccount).where(
            MarketBotAccount.market_id == market.id,
            MarketBotAccount.is_enabled.is_(True),
        )
    )
    add("maker_ready", maker is not None, "an enabled Paper market maker is required")

    bundle = await admin_api.build_strategy_runtime_bundle(session, request, market)
    readiness = admin_api.build_maker_instance_start_readiness(market, bundle)
    add("strategy_ready", bool(readiness.get("ok")), "Configure the selected per-market strategy before starting it")

    return {
        "symbol": market.symbol,
        "product_type": market.product_type,
        "ok": all(item["status"] == "passed" for item in checks),
        "checks": checks,
    }


@router.get("/brand")
async def get_brand(session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    row = await session.scalar(select(PaperBrandConfig).where(PaperBrandConfig.id == 1))
    return {
        "brand": {
            "registration_enabled": bool(settings.paper_exchange_register_enabled),
            "exchange_name": row.exchange_name if row else "Paper Exchange",
            "logo_url": row.logo_url if row else None,
            "favicon_url": row.favicon_url if row else None,
            "primary_color": row.primary_color if row else "#22d3ee",
            "default_language": row.default_language if row else "zh-CN",
            "footer_text": row.footer_text if row else "Paper Trading / 模拟交易",
            "paper_notice": row.paper_notice if row else "所有资产均为模拟资金，不涉及真实资金。",
        },
        "mode": "paper_exchange",
        "notice": "模拟交易，不涉及真实资金",
    }


@router.get("/markets")
async def list_paper_markets(session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    markets = await _paper_markets(session)
    return {"items": [_market_item(market) for market in markets], "persistence": public_persistence_contract()}


@router.get("/markets/{symbol}")
async def get_paper_market(symbol: str, session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None or market.paper_status in {PAPER_MARKET_DRAFT, PAPER_MARKET_VALIDATING, PAPER_MARKET_DELISTED} or not market.is_active:
        raise HTTPException(status_code=404, detail="market not found")
    return _market_item(market)


@router.get("/markets/{symbol}/ticker")
async def paper_ticker(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    return await public.get_ticker(symbol, request, session)


@router.get("/markets/{symbol}/orderbook")
async def paper_orderbook(symbol: str, request: Request, depth: int = Query(default=20, ge=1, le=100)):
    _require_paper_mode()
    return await public.get_orderbook(symbol, request, depth)


@router.get("/markets/{symbol}/maker-instance")
async def paper_maker_instance(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    return await admin_api.get_market_maker_instance(symbol, request, None, session)


# ---------------------------------------------------------------------------
# 做市策略控制面（Paper 后台复用 5174 的 /admin 策略与 maker-instance 能力）
# ---------------------------------------------------------------------------


@router.get("/admin/markets/{symbol}/strategy")
async def paper_admin_strategy(
    symbol: str,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.get_market_strategy(symbol, request, admin, session)


@router.put("/admin/markets/{symbol}/strategy")
async def paper_admin_strategy_update(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.update_market_strategy(symbol, payload, request, admin, session)


@router.get("/admin/markets/{symbol}/strategy/runtime")
async def paper_admin_strategy_runtime(
    symbol: str,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.get_market_strategy_runtime(symbol, request, admin, session)


@router.get("/admin/markets/{symbol}/maker-instance")
async def paper_admin_maker_instance(
    symbol: str,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.get_market_maker_instance(symbol, request, admin, session)


@router.get("/admin/markets/{symbol}/maker-instance/logs")
async def paper_admin_maker_instance_logs(
    symbol: str,
    lines: int = Query(default=120, ge=1, le=500),
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.get_market_maker_instance_logs(symbol, lines, admin, session)


@router.post("/admin/markets/{symbol}/maker-instance/start")
async def paper_admin_maker_instance_start(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    if not payload.get("confirm_execute"):
        raise HTTPException(status_code=400, detail="confirm_execute is required")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return await admin_api.start_maker_instance_for_market(market, request, admin, session)


@router.post("/admin/markets/{symbol}/maker-instance/stop")
async def paper_admin_maker_instance_stop(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    if not payload.get("confirm_execute"):
        raise HTTPException(status_code=400, detail="confirm_execute is required")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    instance = await admin_api.ensure_maker_instance_record(session, market)
    stop_result = admin_api.stop_maker_instance_process(market.symbol)
    if stop_result.get("exited"):
        instance.status = "stopped"
        instance.pid = None
        instance.stopped_at = datetime.now(tz=UTC)
    else:
        instance.status = "stopping"
        instance.pid = stop_result.get("pid")
    await session.commit()
    return {"symbol": market.symbol, "stop": stop_result, "instance": {"status": instance.status}}


@router.post("/admin/markets/{symbol}/maker-instance/restart")
async def paper_admin_maker_instance_restart(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    if not payload.get("confirm_execute"):
        raise HTTPException(status_code=400, detail="confirm_execute is required")
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return await admin_api.clean_restart_maker_instance_for_market(market, request, admin, session)


@router.post("/admin/markets/{symbol}/flow/start")
async def paper_admin_flow_start(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.start_market_flow_robot(symbol, payload, request, admin, session)


@router.post("/admin/markets/{symbol}/flow/pause")
async def paper_admin_flow_pause(
    symbol: str,
    payload: dict,
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    return await admin_api.pause_market_flow_robot(symbol, payload, request, admin, session)


@router.get("/admin/bots")
async def paper_admin_bots(
    request: Request,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Paper 后台机器人观测：每个市场的策略配置、MM 实例与 FLOW 状态、日志尾部。"""
    _require_paper_mode()
    markets = await _paper_markets(session, include_non_trading=True)
    items = []
    for market in markets:
        try:
            bundle = await admin_api.build_strategy_runtime_bundle(session, request, market)
        except Exception as exc:  # 单个市场故障不阻断整页
            items.append({"symbol": market.symbol, "error": str(exc)})
            continue
        instance = await admin_api.ensure_maker_instance_record(session, market)
        strategy_key = admin_api.maker_instance_strategy_key(market, bundle["strategy"]["strategy_key"])
        instance_status = admin_api.maker_instance_status(
            market.symbol,
            request,
            configured_strategy_version=strategy_key,
            instance=instance,
        )
        coordinator = bundle.get("coordinator") if isinstance(bundle.get("coordinator"), dict) else {}
        flow = coordinator.get("flow") if isinstance(coordinator.get("flow"), dict) else {}
        flow_cards = [
            item for item in (bundle.get("robot_cards") or [])
            if isinstance(item, dict) and item.get("role") == "FLOW"
        ]
        items.append(
            {
                "symbol": market.symbol,
                "product_type": market.product_type,
                "strategy": {
                    "strategy_key": bundle["strategy"].get("strategy_key"),
                    "effective_config": bundle["strategy"].get("effective_config"),
                    "bots": bundle.get("bots"),
                },
                "instance": instance_status,
                "flow": {
                    "status": flow.get("status") or (flow_cards[0].get("status") if flow_cards else None),
                    "mode": flow.get("mode"),
                    "allowed": flow.get("allowed"),
                    "pause_reason": flow.get("pause_reason") or flow.get("guard_reason") or flow.get("last_status"),
                },
            }
        )
    await session.commit()
    return {"items": items}


@router.get("/markets/{symbol}/trades")
async def paper_trades(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=200)):
    _require_paper_mode()
    return await public.get_trades(symbol, request, session, limit=limit, include_seed=False)


@router.get("/markets/{symbol}/klines")
async def paper_klines(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    interval: str = "1m",
    limit: int = Query(default=200, ge=1, le=500),
    include_seed: bool = Query(default=True),
):
    _require_paper_mode()
    return await public.get_klines(symbol, request, session, interval=interval, limit=limit, include_seed=include_seed)


@router.get("/markets/{symbol}/mark-price")
async def paper_mark_price(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper(), Market.product_type == PRODUCT_TYPE_PERP))
    if market is None:
        raise HTTPException(status_code=404, detail="perp market not found")
    service = request.app.state.contract_price_service
    state = await service.serialize_market_state(session, market, fetch_external=False)
    await session.commit()
    return state


@router.get("/markets/{symbol}/funding-rate")
async def paper_funding_rate(symbol: str, request: Request, session: AsyncSession = Depends(get_db_session)):
    """Return the current durable PaperTrading funding/mark state."""
    _require_paper_mode()
    market = await session.scalar(
        select(Market).where(Market.symbol == symbol.upper(), Market.product_type == PRODUCT_TYPE_PERP)
    )
    if market is None or market.paper_status == PAPER_MARKET_DELISTED:
        raise HTTPException(status_code=404, detail="perp market not found")
    state = await request.app.state.contract_price_service.serialize_market_state(
        session, market, fetch_external=False
    )
    await session.commit()
    return state


@router.get("/account")
async def paper_account(request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    spot = await request.app.state.order_service.serialize_balances(session, user.id)
    perp = await request.app.state.contract_service.serialize_account(session, user.id, "USDT")
    spot = [{**item, "available": _paper_decimal(item.get("available")), "frozen": _paper_decimal(item.get("frozen"))} for item in spot]
    return {"user": serialize_user(user), "spot": {"balances": spot}, "perp": {"account": _clean_account_payload(perp)}}


@router.get("/account/spot")
async def paper_spot_account(request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    balances = await request.app.state.order_service.serialize_balances(session, user.id)
    return {"balances": [{**item, "available": _paper_decimal(item.get("available")), "frozen": _paper_decimal(item.get("frozen"))} for item in balances]}


@router.get("/account/perp")
async def paper_perp_account(request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    service: ContractService = request.app.state.contract_service
    account = _clean_account_payload(await service.serialize_account(session, user.id, "USDT"))
    markets = await _paper_markets(session)
    positions = []
    for market in markets:
        if market.product_type != PRODUCT_TYPE_PERP:
            continue
        position = await service.get_position(session, user.id, market, create=False)
        if position is not None and position.is_active:
            positions.append(await service.serialize_position(position, market, session))
    return {"account": account, "positions": positions}


@router.get("/account/runs")
async def paper_account_runs(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    rows = await session.execute(select(PaperAccountRun).where(PaperAccountRun.user_id == user.id).order_by(PaperAccountRun.created_at.desc()))
    return {
        "items": [
            {
                "run_id": row.run_id,
                "account_epoch": row.account_epoch,
                "status": row.status,
                "scope": row.scope,
                "reason": row.reason,
                "created_at": to_millis(row.created_at),
                "ended_at": to_millis(row.ended_at) if row.ended_at else None,
            }
            for row in rows.scalars()
        ]
    }


@router.post("/account/reset")
async def reset_my_account(request: Request, payload: PaperResetRequest, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    markets = await _paper_markets(session, include_non_trading=True)
    return await reset_user_account(
        session,
        user,
        markets=markets,
        runtime=request.app.state.runtime,
        order_service=request.app.state.order_service,
        contract_service=request.app.state.contract_service,
        actor_user_id=user.id,
        reason=payload.reason,
    )


async def _create_spot_order(request: Request, payload: OrderCreateRequest, user: User, session: AsyncSession):
    try:
        return await request.app.state.order_service.place_order(session, user, payload)
    except (OrderValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc) from exc


@router.post("/spot/orders")
async def create_paper_spot_order(payload: OrderCreateRequest, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    try:
        # Paper user orders settle directly and durably on the slow path.
        # The write-behind replay of fast-path fills validates robot maker
        # pre-fill anchors against the durable order history; a user order is
        # allowed to interleave with QuoteSet churn at any moment, so a
        # fill-bearing replay task can arrive with a stale anchor and halt the
        # fail-closed materializer, freezing every subsequent user event in
        # the durable view.  User orders are low-frequency; the slow path
        # keeps orders, trades, balances and ledger consistent immediately and
        # generates no replay task for the user side.  Robot QuoteSets keep
        # the fast path by design (restart-ephemeral liquidity).
        if str(user.role) != "bot":
            return await _create_spot_order(request, payload, user, session)
        fast_result = await request.app.state.order_service.place_order_fast(session, user, payload)
        if fast_result is not None:
            return fast_result
        return await _create_spot_order(request, payload, user, session)
    except (OrderValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc) from exc


@router.post("/perp/orders")
async def create_paper_perp_order(payload: ContractOrderCreateRequest, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    try:
        # Paper user orders always use the durable slow path (see the spot
        # order comment for the full rationale).  The in-memory fast
        # settlement path also treats every OPEN fill as one-way and would
        # reject the hedge-mode Paper market maker when its existing position
        # is on the opposite side, which breaks every user close once the
        # maker has inventory.  Robot QuoteSets keep the fast path by design.
        if str(user.role) != "bot":
            if str(payload.position_action or "open") == "close" or bool(payload.reduce_only):
                return await _place_paper_contract_close(request, session, user, payload)
            return await request.app.state.contract_service.place_order(session, user, payload)
        fast_result = await request.app.state.contract_service.place_order_fast(session, user, payload)
        if fast_result is not None:
            return fast_result
        return await request.app.state.contract_service.place_order(session, user, payload)
    except (ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc) from exc


@router.post("/perp/positions/{symbol}/close")
async def close_paper_perp_position(
    symbol: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Market-close the user's position on one USDT-margined Paper perp market.

    The close uses the normal durable reduce-only IOC path so margin release,
    realized PnL, trades and ledger evidence are all produced by the existing
    contract service rather than a UI-side shortcut.
    """
    _require_paper_mode()
    market = await session.scalar(
        select(Market).where(Market.symbol == symbol.upper(), Market.product_type == PRODUCT_TYPE_PERP)
    )
    if market is None or market.paper_status == PAPER_MARKET_DELISTED:
        raise HTTPException(status_code=404, detail="perp market not found")
    service: ContractService = request.app.state.contract_service
    await drain_writer(request)
    try:
        await wait_contract_position_converged(request, session, user, market, timeout_seconds=10.0)
    except ContractValidationError:
        await session.rollback()
    try:
        position = await service.get_position(session, user.id, market, create=False)
    except ContractValidationError as exc:
        await session.rollback()
        raise _error(exc, "NO_POSITION") from exc
    if position is None or Decimal(position.quantity or ZERO) <= ZERO:
        raise HTTPException(status_code=404, detail="no position to close")
    side = "sell" if str(position.side).lower() == "long" else "buy"
    payload = ContractOrderCreateRequest(
        symbol=market.symbol,
        side=side,
        type="market",
        tif="ioc",
        quantity=Decimal(position.quantity or ZERO),
        position_action="close",
        reduce_only=True,
        leverage=Decimal(position.leverage or market.default_leverage or "1"),
        client_order_id=f"paper-close-{user.id}-{to_millis(datetime.now(tz=UTC))}",
    )
    try:
        result = await _place_paper_contract_close(request, session, user, payload)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        await session.rollback()
        raise _error(exc, "CLOSE_REJECTED") from exc
    remaining = await service.get_position(session, user.id, market, create=False)
    return {
        "order": result.get("order") if isinstance(result, dict) else result,
        "position": (
            await service.serialize_position(remaining, market, session) if remaining is not None and remaining.is_active else None
        ),
    }


@router.get("/perp/settings/{symbol}")
async def get_paper_perp_setting(
    symbol: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    try:
        market = await request.app.state.contract_service.get_market(session, symbol)
        setting = await request.app.state.contract_service.get_setting(session, user.id, market)
        return await request.app.state.contract_service.serialize_setting(setting, market)
    except ContractValidationError as exc:
        await session.rollback()
        raise _error(exc, "INVALID_PERP_MARKET") from exc


@router.put("/perp/settings/{symbol}")
async def update_paper_perp_setting(
    symbol: str,
    payload: ContractLeverageUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    try:
        market = await request.app.state.contract_service.get_market(session, symbol)
        setting = await request.app.state.contract_service.update_setting(
            session,
            user,
            market,
            leverage=payload.leverage,
            margin_mode=payload.margin_mode,
            position_mode=payload.position_mode,
        )
        return await request.app.state.contract_service.serialize_setting(setting, market)
    except ContractValidationError as exc:
        await session.rollback()
        raise _error(exc, "INVALID_PERP_SETTING") from exc


@router.get("/orders")
async def list_paper_orders(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session), status_filter: str | None = Query(default=None, alias="status"), product_type: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
    stmt = select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.user_id == user.id)
    if status_filter:
        stmt = stmt.where(Order.status == status_filter)
    if product_type:
        stmt = stmt.where(Order.product_type == product_type.upper())
    rows = await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
    items = []
    for order, market in rows.all():
        items.append(_order_item(order, market))
    return {"items": items}


def _order_item(order: Order, market: Market) -> dict:
    return {
        "order_id": order.order_id,
        "client_order_id": order.client_order_id,
        "symbol": market.symbol,
        "product_type": order.product_type,
        "side": order.side,
        "position_action": order.position_action,
        "reduce_only": bool(order.reduce_only),
        "type": order.type,
        "tif": order.tif,
        "status": order.status,
        "price": _paper_decimal(order.price, int(market.price_precision)),
        "quantity": _paper_decimal(order.quantity, int(market.qty_precision)),
        "filled_quantity": _paper_decimal(order.filled_quantity, int(market.qty_precision)),
        "remaining_quantity": _paper_decimal(order.remaining_quantity, int(market.qty_precision)),
        "avg_price": _paper_decimal(order.avg_price, int(market.price_precision)),
        "leverage": _paper_decimal(order.leverage),
        "notional": _paper_decimal(order.notional),
        "reject_reason": order.reject_reason,
        "created_at": to_millis(order.created_at),
        "updated_at": to_millis(order.updated_at),
    }


@router.get("/orders/open")
async def list_paper_open_orders(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    rows = await session.execute(
        select(Order, Market)
        .join(Market, Market.id == Order.market_id)
        .where(Order.user_id == user.id, Order.status.in_(["new", "partially_filled"]))
        .order_by(Order.created_at.desc())
    )
    return {"items": [_order_item(order, market) for order, market in rows.all()]}


@router.patch("/orders/{order_id}")
async def amend_paper_order(
    order_id: str,
    payload: OrderAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    _require_paper_mode()
    row = await session.execute(
        select(Order, Market)
        .join(Market, Market.id == Order.market_id)
        .where(Order.order_id == order_id, Order.user_id == user.id)
    )
    record = row.first()
    if record is None:
        raise HTTPException(status_code=404, detail="order not found")
    _order, market = record
    try:
        if market.product_type == PRODUCT_TYPE_SPOT:
            return await _retry_engine_miss(
                request,
                session,
                market,
                lambda: request.app.state.order_service.amend_order(session, user, order_id, payload),
                error_text="order not found on book",
            )
        contract_payload = ContractOrderAmendRequest(quantity=payload.quantity, price=payload.price)
        return await _retry_engine_miss(
            request,
            session,
            market,
            lambda: request.app.state.contract_service.amend_order(session, user, order_id, contract_payload),
            error_text="order not found on book",
        )
    except (OrderValidationError, ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc, "ORDER_NOT_AMENDABLE") from exc


@router.delete("/orders")
async def cancel_all_paper_orders(
    request: Request,
    symbol: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Cancel every open order for one PaperTrading market."""
    _require_paper_mode()
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None or market.paper_status == PAPER_MARKET_DELISTED:
        raise HTTPException(status_code=404, detail="market not found")
    try:
        if market.product_type == PRODUCT_TYPE_SPOT:
            fast_result = await request.app.state.order_service.cancel_all_fast(session, user, market.symbol)
            if fast_result is not None:
                return fast_result
            return await request.app.state.order_service.cancel_all(session, user, market.symbol)

        fast_orders = getattr(request.app.state.contract_service, "_fast_contract_orders", {})
        order_ids = [
            str(order_id)
            for order_id, snap in fast_orders.items()
            if int(snap.get("user_id") or -1) == int(user.id)
            and int(snap.get("market_id") or -1) == int(market.id)
            and str(snap.get("status") or "") in {"new", "partially_filled"}
        ]
        if not order_ids:
            rows = await session.execute(
                select(Order.order_id)
                .where(
                    Order.user_id == user.id,
                    Order.market_id == market.id,
                    Order.product_type == PRODUCT_TYPE_PERP,
                    Order.status.in_(["new", "partially_filled"]),
                )
                .order_by(Order.created_at.asc())
            )
            order_ids = [str(order_id) for (order_id,) in rows.all()]
        canceled = 0
        failed: list[dict[str, str]] = []
        for order_id in order_ids:
            try:
                fast_result = await request.app.state.contract_service.cancel_order_fast(session, user, order_id)
                if fast_result is None:
                    await request.app.state.contract_service.cancel_order(session, user, order_id)
                canceled += 1
            except (ContractValidationError, ValueError) as exc:
                await session.rollback()
                failed.append({"order_id": order_id, "message": str(exc)})
        return {"count": canceled, "failed": failed}
    except (OrderValidationError, ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc, "ORDER_NOT_CANCELABLE") from exc


@router.delete("/orders/{order_id}")
async def cancel_paper_order(order_id: str, request: Request, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session)):
    row = await session.execute(select(Order, Market).join(Market, Market.id == Order.market_id).where(Order.order_id == order_id, Order.user_id == user.id))
    record = row.first()
    if record is None:
        raise HTTPException(status_code=404, detail="order not found")
    order, market = record
    try:
        if market.product_type == PRODUCT_TYPE_SPOT:
            return await _retry_engine_miss(
                request,
                session,
                market,
                lambda: request.app.state.order_service.cancel_order(session, user, order_id),
                error_text="order not found on book",
            )
        return await _retry_engine_miss(
            request,
            session,
            market,
            lambda: request.app.state.contract_service.cancel_order(session, user, order_id),
            error_text="order not found on book",
        )
    except (OrderValidationError, ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise _error(exc, "ORDER_NOT_CANCELABLE") from exc


@router.get("/trades")
async def list_paper_trades(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=500)):
    rows = await session.execute(
        select(Trade, Market)
        .join(Market, Market.id == Trade.market_id)
        .where(or_(Trade.taker_user_id == user.id, Trade.maker_user_id == user.id))
        .order_by(Trade.executed_at.desc())
        .limit(limit)
    )
    items = []
    for trade, market in rows.all():
        service = getattr(session, "paper_service", None)
        is_taker = trade.taker_user_id == user.id
        items.append(
            {
                "trade_id": trade.trade_id,
                "symbol": market.symbol,
                "product_type": trade.product_type,
                "side": trade.taker_side if is_taker else ("sell" if trade.taker_side == "buy" else "buy"),
                "price": _paper_decimal(trade.price, int(market.price_precision)),
                "quantity": _paper_decimal(trade.quantity, int(market.qty_precision)),
                "quote_amount": _paper_decimal(trade.quote_amount),
                "fee": _paper_decimal(trade.taker_fee if is_taker else trade.maker_fee),
                "fee_asset": trade.fee_asset_taker if is_taker else trade.fee_asset_maker,
                "realized_pnl": decimal_to_str(trade.taker_realized_pnl if is_taker else trade.maker_realized_pnl),
                "source": trade.source,
                "ts": to_millis(trade.executed_at),
            }
        )
    return {"items": items}


@router.get("/ledger")
async def list_paper_ledger(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=500)):
    from app.models.ledger_entry import LedgerEntry

    spot_rows = await session.execute(select(LedgerEntry).where(LedgerEntry.user_id == user.id).order_by(LedgerEntry.created_at.desc()).limit(limit))
    contract_rows = await session.execute(select(ContractLedgerEntry).where(ContractLedgerEntry.user_id == user.id).order_by(ContractLedgerEntry.created_at.desc()).limit(limit))
    return {
        "spot": [
            {"entry_id": row.entry_id, "asset": row.asset, "change_type": row.change_type, "amount": decimal_to_str(row.amount), "balance_after": decimal_to_str(row.balance_after), "created_at": to_millis(row.created_at)}
            for row in spot_rows.scalars()
        ],
        "contract": [
            {"entry_id": row.entry_id, "margin_asset": row.margin_asset, "change_type": row.change_type, "amount": decimal_to_str(row.amount), "wallet_after": decimal_to_str(row.wallet_after), "available_after": decimal_to_str(row.available_after), "created_at": to_millis(row.created_at)}
            for row in contract_rows.scalars()
        ],
    }


@router.get("/funding")
async def list_paper_funding(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=500)):
    rows = await session.execute(select(ContractFundingEvent).where(ContractFundingEvent.user_id == user.id).order_by(ContractFundingEvent.created_at.desc()).limit(limit))
    return {"items": [{"event_id": row.event_id, "market_id": row.market_id, "amount": decimal_to_str(row.amount), "rate": decimal_to_str(row.funding_rate), "created_at": to_millis(row.created_at)} for row in rows.scalars()]}


@router.get("/liquidations")
async def list_paper_liquidations(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_db_session), limit: int = Query(default=100, ge=1, le=500)):
    rows = await session.execute(select(ContractLiquidationEvent).where(ContractLiquidationEvent.user_id == user.id).order_by(ContractLiquidationEvent.created_at.desc()).limit(limit))
    return {"items": [{"event_id": row.event_id, "market_id": row.market_id, "quantity": decimal_to_str(row.quantity), "realized_pnl": decimal_to_str(row.realized_pnl), "reason": row.reason, "created_at": to_millis(row.created_at)} for row in rows.scalars()]}


@router.get("/admin/overview")
async def paper_admin_overview(request: Request, admin: User = Depends(get_admin_user), session: AsyncSession = Depends(get_db_session)):
    _require_paper_mode()
    users = int(await session.scalar(select(func.count()).select_from(User).where(User.role != "admin")) or 0)
    markets = await _paper_markets(session, include_non_trading=True)
    open_orders = int(await session.scalar(select(func.count()).select_from(Order).where(Order.status.in_(["new", "partially_filled"]))) or 0)
    trades = int(await session.scalar(select(func.count()).select_from(Trade)) or 0)
    return {"mode": "paper_exchange", "users": users, "markets": len(markets), "open_orders": open_orders, "trades": trades, "liquidity": {"owner": "strategy_plugins", "legacy_retired": True}, "global_run_id": getattr(request.app.state.runtime, "paper_global_run_id", None), "persistence": public_persistence_contract(run_id=request.app.state.runtime.run_id)}


@router.get("/admin/markets")
async def paper_admin_markets(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    return {"items": [_market_item(market) for market in await _paper_markets(session, include_non_trading=True)]}


@router.post("/admin/markets", status_code=status.HTTP_201_CREATED)
async def create_paper_market(payload: PaperMarketCreateRequest, request: Request, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    raise HTTPException(status_code=410, detail="旧版内置铺单已退役，请使用交易币对管理与铺单策略页面（/admin/liquidity/makers）")


def _recommend_tick_lot(reference_price: Decimal) -> tuple[Decimal, Decimal]:
    """Auto-recommend tick/lot (PRD 8.3, refined for extreme prices).

    tick follows a 1/2/5 ladder sized at ~0.01% of the price so the default
    4bps level spacing never collapses into one tick (e.g. PEPE at ~2.7e-6
    needs a ~5e-10 tick, while BTC around 62000 gets 10).  lot is a 1/2/5
    ladder so one lot stays near or above 5 USDT notional.
    """
    price = max(Decimal(str(reference_price or "0")), Decimal("0.000000001"))
    tick_target = max(price * Decimal("0.0001"), Decimal("0.000000001"))
    tick_exp = tick_target.adjusted()
    tick_magnitude = Decimal(10) ** tick_exp
    tick = tick_magnitude
    for candidate in (Decimal("1"), Decimal("2"), Decimal("5")):
        if candidate * tick_magnitude >= tick_target:
            tick = candidate * tick_magnitude
            break
    else:
        tick = Decimal(10) ** (tick_exp + 1)
    raw_lot = Decimal("5") / price
    exponent = raw_lot.adjusted()
    magnitude = Decimal(10) ** exponent
    lot = magnitude
    for candidate in (Decimal("1"), Decimal("2"), Decimal("5")):
        if candidate * magnitude >= raw_lot:
            lot = candidate * magnitude
            break
    else:
        lot = Decimal(10) ** (exponent + 1)
    return tick, lot




class PaperListingRequest(BaseModel):
    """极简上币：一个币名 + 价格源即可完成现货/永续上币并自动开市。

    - ``price_source=binance``：行情跟随 Binance 公共行情（现货用 spot ticker，
      永续用 futures premiumIndex 指数价），只读公共接口，不需要任何密钥；
    - ``price_source=manual``：单机固定参考价，需提供 ``reference_price``；
    - ``price_source=simulated``：单机随机游走模拟行情（自有币种演示）。
    """

    base_asset: str = Field(min_length=2, max_length=16, description="资产代码，例如 PEPE")
    display_name: str | None = Field(default=None, max_length=64, description="资产展示名称，默认同代码")
    price_source: Literal["binance", "manual", "simulated"] = "simulated"
    binance_symbol: str | None = Field(default=None, max_length=24, description="Binance 交易对，默认 <BASE>USDT")
    reference_price: Decimal | None = Field(default=None, gt=0, description="manual 必填；binance/simulated 可选兜底价")
    products: list[Literal["SPOT", "PERP"]] = Field(default=["SPOT", "PERP"])
    max_leverage: Decimal = Field(default=Decimal("20"), gt=0, le=100, description="永续最大杠杆，默认 20x")
    auto_open: bool = Field(default=True, description="校验通过后自动 PRE_OPEN→TRADING")


@router.post("/admin/listing", status_code=status.HTTP_201_CREATED)
async def paper_admin_listing(
    payload: PaperListingRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    raise HTTPException(status_code=410, detail="旧版内置铺单已退役，请使用交易币对管理与铺单策略页面（/admin/liquidity/makers）")


@router.get("/admin/listing/status")
async def paper_listing_status(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    raise HTTPException(status_code=410, detail="旧版内置铺单已退役，请使用交易币对管理与铺单策略页面（/admin/liquidity/makers）")


@router.patch("/admin/markets/{symbol}")
async def update_paper_market(
    symbol: str,
    payload: PaperMarketUpdateRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    _require_paper_mode()
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    if payload.status is not None:
        target = payload.status.upper()
        if target not in PAPER_STATUSES:
            raise _error(ValueError("invalid market status"), "INVALID_MARKET_STATE")
        current = str(market.paper_status or PAPER_MARKET_TRADING)
        if target != current and target not in STATUS_TRANSITIONS.get(current, set()):
            raise _error(ValueError(f"invalid transition {current}->{target}"), "INVALID_MARKET_STATE")
        if target == PAPER_MARKET_REDUCE_ONLY and market.product_type != PRODUCT_TYPE_PERP:
            raise _error(ValueError("spot market does not support REDUCE_ONLY"), "INVALID_MARKET_STATE")
        market.paper_status = target
        if market.product_type == PRODUCT_TYPE_PERP:
            if target in {PAPER_MARKET_REDUCE_ONLY, PAPER_MARKET_DELISTING}:
                market.contract_trading_mode = "reduce_only"
            elif target == PAPER_MARKET_TRADING:
                market.contract_trading_mode = "normal"
        if target == PAPER_MARKET_DELISTING:
            open_rows = await session.execute(
                select(Order.order_id, Order.position_action).where(
                    Order.market_id == market.id,
                    Order.status.in_(["new", "partially_filled"]),
                )
            )
            # Spot delisting removes every order.  Perp delisting keeps only
            # reduce/close orders so the remaining position can be settled.
            for order_id, position_action in open_rows.all():
                if market.product_type == PRODUCT_TYPE_PERP and str(position_action) == "close":
                    continue
                try:
                    if market.product_type == PRODUCT_TYPE_SPOT:
                        await request.app.state.order_service.cancel_order(
                            session, admin, str(order_id), admin_override=True
                        )
                    else:
                        await request.app.state.contract_service.cancel_order(
                            session, admin, str(order_id), admin_override=True
                        )
                except (OrderValidationError, ContractValidationError, ValueError):
                    await session.rollback()
        if target == PAPER_MARKET_DELISTED:
            market.is_active = False
    if payload.price_source is not None:
        market.price_source = payload.price_source
    if payload.reference_price is not None:
        market.reference_price = payload.reference_price
    if payload.price_source_symbol is not None:
        market.price_source_symbol = payload.price_source_symbol
    if payload.price_protection_pct is not None:
        market.price_protection_pct = payload.price_protection_pct
    if payload.enabled is not None:
        market.is_active = payload.enabled
    await session.commit()
    return _market_item(market)


@router.post("/admin/markets/{symbol}/validate")
async def validate_paper_market(
    symbol: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    _require_paper_mode()
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None or market.paper_status == PAPER_MARKET_DELISTED:
        raise HTTPException(status_code=404, detail="market not found")
    if market.paper_status == PAPER_MARKET_DRAFT:
        market.paper_status = PAPER_MARKET_VALIDATING
    result = await _validate_paper_market(request, session, market)
    if result["ok"]:
        market.paper_status = PAPER_MARKET_PRE_OPEN
    await session.commit()
    result["paper_status"] = market.paper_status
    return result


@router.get("/admin/assets")
async def paper_admin_assets(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    rows = await session.execute(select(PaperAsset).order_by(PaperAsset.code.asc()))
    return {"items": [{"id": row.id, "code": row.code, "display_name": row.display_name, "icon": row.icon, "description": row.description, "status": row.status, "precision": row.display_precision} for row in rows.scalars()]}


@router.post("/admin/assets")
async def create_paper_asset(payload: dict, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    code = str(payload.get("code") or "").upper().strip()
    if not code or await session.scalar(select(PaperAsset.id).where(PaperAsset.code == code)) is not None:
        raise HTTPException(status_code=409, detail="asset already exists or code is empty")
    row = PaperAsset(code=code, display_name=str(payload.get("display_name") or code), description=str(payload.get("description") or "")[:255] or None, display_precision=int(payload.get("display_precision") or 8), status="ACTIVE")
    session.add(row)
    await session.commit()
    return {"code": row.code, "display_name": row.display_name, "status": row.status}


@router.patch("/admin/assets/{asset_id}")
async def update_paper_asset(
    asset_id: int,
    payload: PaperAssetUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    row = await session.get(PaperAsset, asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail="asset not found")
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    await session.commit()
    return {"id": row.id, "code": row.code, "display_name": row.display_name, "status": row.status, "precision": row.display_precision}


@router.get("/admin/users")
async def paper_admin_users(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    rows = await session.execute(select(User).order_by(User.created_at.desc()))
    return {"items": [serialize_user(user) for user in rows.scalars()]}


@router.get("/admin/users/{user_id}")
async def paper_admin_user_detail(
    user_id: int,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    balances = await request.app.state.order_service.serialize_balances(session, user.id)
    contract = await request.app.state.contract_service.serialize_account(session, user.id, "USDT")
    return {"user": serialize_user(user), "spot": {"balances": balances}, "perp": {"account": contract}}


async def _set_paper_user_active(
    user_id: int,
    active: bool,
    session: AsyncSession,
    admin: User,
) -> dict:
    user = await session.get(User, user_id)
    if user is None or user.role == "admin":
        raise HTTPException(status_code=404, detail="user not found")
    user.is_active = active
    if not active:
        await session.execute(delete(PaperSession).where(PaperSession.user_id == user.id))
    await session.commit()
    return {"user": serialize_user(user), "is_active": bool(user.is_active)}


@router.post("/admin/users/{user_id}/enable")
async def paper_admin_enable_user(user_id: int, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    return await _set_paper_user_active(user_id, True, session, admin)


@router.post("/admin/users/{user_id}/disable")
async def paper_admin_disable_user(user_id: int, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    return await _set_paper_user_active(user_id, False, session, admin)


@router.post("/admin/users/{user_id}/reset")
async def paper_admin_reset_user(user_id: int, payload: PaperResetRequest, request: Request, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.role == "admin":
        raise HTTPException(status_code=404, detail="user not found")
    markets = await _paper_markets(session, include_non_trading=True)
    return await reset_user_account(session, user, markets=markets, runtime=request.app.state.runtime, order_service=request.app.state.order_service, contract_service=request.app.state.contract_service, actor_user_id=admin.id, reason=payload.reason)


@router.post("/admin/reset-all")
async def paper_admin_reset_all(payload: PaperResetRequest, request: Request, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    markets = await _paper_markets(session, include_non_trading=True)
    users = list((await session.execute(select(User).where(User.role != "admin"))).scalars())
    return await reset_all_accounts(session, markets=markets, users=users, runtime=request.app.state.runtime, order_service=request.app.state.order_service, contract_service=request.app.state.contract_service, actor_user_id=admin.id, reason=payload.reason)


@router.get("/admin/liquidity")
async def paper_admin_liquidity(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    raise HTTPException(status_code=410, detail="旧版内置铺单已退役，请使用交易币对管理与铺单策略页面（/admin/liquidity/makers）")


@router.patch("/admin/liquidity/{symbol}")
async def update_paper_liquidity(symbol: str, payload: PaperLiquidityUpdateRequest, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    raise HTTPException(status_code=410, detail="旧版内置铺单已退役，请使用交易币对管理与铺单策略页面（/admin/liquidity/makers）")


@router.patch("/admin/brand")
async def update_paper_brand(payload: PaperBrandUpdateRequest, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    row = await session.scalar(select(PaperBrandConfig).where(PaperBrandConfig.id == 1))
    if row is None:
        row = PaperBrandConfig(id=1)
        session.add(row)
    for key, value in payload.model_dump(exclude_none=True).items():
        setattr(row, key, value)
    await session.commit()
    return {"updated": True}


@router.get("/admin/defaults")
async def get_paper_defaults(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    row = await session.scalar(select(PaperSystemSetting).where(PaperSystemSetting.key == "paper_defaults"))
    if row is None or not isinstance(row.value_json, dict):
        return {
            "spot_initial_usdt": decimal_to_str(settings.paper_exchange_default_spot_usdt),
            "perp_initial_usdt": decimal_to_str(settings.paper_exchange_default_perp_usdt),
            "default_leverage": "5",
            "user_reset_enabled": True,
        }
    return row.value_json


@router.patch("/admin/defaults")
async def update_paper_defaults(
    payload: PaperDefaultsUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
    admin: User = Depends(get_admin_user),
):
    row = await session.scalar(select(PaperSystemSetting).where(PaperSystemSetting.key == "paper_defaults"))
    if row is None:
        row = PaperSystemSetting(key="paper_defaults", value_json={})
        session.add(row)
    current = dict(row.value_json) if isinstance(row.value_json, dict) else {}
    for key, value in payload.model_dump(exclude_none=True).items():
        current[key] = str(value) if isinstance(value, Decimal) else value
    row.value_json = current
    await session.commit()
    return current


@router.get("/admin/system")
async def paper_admin_system(request: Request, session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user)):
    rows = await session.execute(select(PaperSystemSetting).order_by(PaperSystemSetting.key.asc()))
    global_run = await session.scalar(select(PaperGlobalRun).where(PaperGlobalRun.id == 1))
    return {"settings": {row.key: row.value_json for row in rows.scalars()}, "global_run": {"run_id": global_run.run_id, "global_epoch": global_run.global_epoch} if global_run else None, "health": {"mode": "paper_exchange", "persistence": public_persistence_contract(run_id=request.app.state.runtime.run_id)}}


@router.get("/admin/resets")
async def paper_admin_resets(session: AsyncSession = Depends(get_db_session), admin: User = Depends(get_admin_user), limit: int = Query(default=100, ge=1, le=500)):
    rows = await session.execute(select(PaperResetRecord).order_by(PaperResetRecord.created_at.desc()).limit(limit))
    return {"items": [{"id": row.id, "scope": row.scope, "user_id": row.user_id, "actor_user_id": row.actor_user_id, "old_run_id": row.old_run_id, "new_run_id": row.new_run_id, "old_epoch": row.old_epoch, "new_epoch": row.new_epoch, "reason": row.reason, "created_at": to_millis(row.created_at)} for row in rows.scalars()]}
