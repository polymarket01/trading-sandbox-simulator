from __future__ import annotations

from app.services.matching_faults import NotExecuted
from app.api.causal_helpers import matching_ack_response

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_admin_user, get_current_user
from app.api.durable_helpers import drain_writer, wait_contract_position_converged
from app.core.config import settings
from app.core.constants import (
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    PRODUCT_TYPE_PERP,
    ROLE_BOT,
)
from app.db.session import get_db_session
from app.models.contract_account import ContractAccount
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_funding_job import ContractFundingJob
from app.models.contract_funding_settlement import ContractFundingSettlement
from app.models.contract_insurance_event import ContractInsuranceEvent
from app.models.contract_insurance_fund import ContractInsuranceFund
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_position import ContractPosition
from app.models.contract_risk_limit_tier import ContractRiskLimitTier
from app.models.market import Market
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.schemas.api import (
    ConfirmExecuteRequest,
    ContractAccountAdjustRequest,
    ContractAdlExecuteRequest,
    ContractInsuranceFundAdjustRequest,
    ContractLeverageUpdateRequest,
    ContractOrderAmendRequest,
    ContractOrderBatchAmendRequest,
    ContractOrderBatchCreateRequest,
    ContractOrderCreateRequest,
    ContractRiskLimitTierUpdateRequest,
    SeedMarketBookRequest,
)
from app.schemas.quote_set import QuoteSetReplaceRequest
from app.services.contract_adl import (
    build_contract_adl_candidates,
    execute_contract_adl,
    serialize_adl_candidate,
    serialize_adl_event,
)
from app.services.contract_insurance import (
    apply_contract_insurance_change,
    get_contract_insurance_fund,
    serialize_contract_insurance_event,
    serialize_contract_insurance_fund,
)
from app.services.contract_price_service import ContractPriceService
from app.services.contract_ledger import serialize_contract_ledger_entry
from app.services.contract_service import ContractService, ContractValidationError
from app.services.quote_set_service import QuoteSetService
from app.services.order_service import OrderValidationError
from app.services.contract_maintenance_service import ContractMaintenanceService
from app.services.contract_liquidity_service import ContractLiquidityService
from app.services.admin_operation_audit import record_admin_operation
from app.services.synthetic_flow_service import SyntheticFlowUnavailableError
from app.services.persistence_contract import legacy_sampled_runtime
from app.api.causal_helpers import (
    begin_causal_command,
    causal_command_context,
    complete_causal_command,
    existing_response_or_raise,
    mark_unknown_causal,
    reject_causal_command,
)

router = APIRouter(tags=["contracts"])


def get_contract_service(request: Request) -> ContractService:
    return request.app.state.contract_service


def get_quote_set_service(request: Request) -> QuoteSetService:
    return request.app.state.quote_set_service


def get_contract_price_service(request: Request) -> ContractPriceService:
    return request.app.state.contract_price_service


def get_contract_maintenance_service(request: Request) -> ContractMaintenanceService:
    return request.app.state.contract_maintenance_service


def get_contract_liquidity_service(request: Request) -> ContractLiquidityService:
    return request.app.state.contract_liquidity_service


@router.get("/contracts/markets")
async def list_contract_markets(request: Request, session: AsyncSession = Depends(get_db_session)):
    price_service = get_contract_price_service(request)
    rows = await session.execute(
        select(Market).where(Market.product_type == PRODUCT_TYPE_PERP).order_by(Market.symbol.asc())
    )
    items = []
    for market in rows.scalars():
        price_state = await price_service.serialize_market_state(session, market, fetch_external=False, persist=False)
        items.append(serialize_contract_market(market, price_state))
    return {"items": items}


def serialize_contract_market(market: Market, price_state: dict | None = None) -> dict:
    return {
        "symbol": market.symbol,
        "price_source": market.price_source,
        "price_source_symbol": market.price_source_symbol,
        "product_type": market.product_type,
        "market_type": market.market_type,
        "base_asset": market.base_asset,
        "quote_asset": market.quote_asset,
        "margin_asset": market.margin_asset,
        "price_tick": str(market.price_tick),
        "qty_step": str(market.qty_step),
        "min_qty": str(market.min_qty),
        "min_notional": str(market.min_notional),
        "max_leverage": str(market.max_leverage),
        "default_leverage": str(market.default_leverage),
        "maintenance_margin_rate": str(market.maintenance_margin_rate),
        "funding_rate": price_state["funding_rate"] if price_state is not None else str(market.funding_rate),
        "funding_interval_hours": market.funding_interval_hours,
        "index_price_source": market.index_price_source,
        "mark_price_mode": market.mark_price_mode,
        "funding_rate_mode": market.funding_rate_mode,
        "funding_interest_rate": str(market.funding_interest_rate),
        "funding_clamp_rate": str(market.funding_clamp_rate),
        "funding_cap_rate": str(market.funding_cap_rate),
        "funding_impact_notional": str(market.funding_impact_notional),
        "contract_trading_mode": market.contract_trading_mode,
        "reference_price": str(market.reference_price) if market.reference_price is not None else None,
        "price_precision": market.price_precision,
        "qty_precision": market.qty_precision,
        "is_active": market.is_active,
        "price_state": price_state,
    }


@router.get("/contracts/prices/{symbol}")
async def get_contract_price_state(
    symbol: str,
    request: Request,
    refresh_external: bool = Query(default=True),
    session: AsyncSession = Depends(get_db_session),
):
    price_service = get_contract_price_service(request)
    contract_service = get_contract_service(request)
    try:
        market = await contract_service.get_market(session, symbol)
        state = await price_service.serialize_market_state(session, market, fetch_external=refresh_external, persist=False)
        return state
    except (ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/contracts/markets/{symbol}/seed-book")
async def admin_seed_contract_market_book(
    symbol: str,
    payload: SeedMarketBookRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    contract_service = get_contract_service(request)
    liquidity_service = get_contract_liquidity_service(request)
    try:
        market = await contract_service.get_market(session, symbol)
        response = await liquidity_service.seed_market_book(session, market, payload, now=datetime.now(tz=UTC))
        if not payload.dry_run:
            await record_admin_operation(
                session,
                actor=admin_user,
                request=request,
                domain="contract",
                operation_type="seed_contract_orderbook",
                target_type="market",
                target_id=market.id,
                target_symbol=market.symbol,
                status="success",
                summary=f"初始化 {market.symbol} 合约测试盘口",
                result=response,
            )
            await session.commit()
        return response
    except ContractValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/contracts/account")
async def get_contract_account(
    request: Request,
    margin_asset: str = Query(default="USDT"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    return await service.serialize_account(session, user.id, margin_asset)


@router.get("/contracts/runtime-state")
async def get_contract_runtime_state(
    request: Request,
    symbol: str,
    margin_asset: str = Query(default="USDT"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """Return one atomic account/position view for the external PERP maker.

    Synthetic FLOW never changes accounts or positions.  Robot quote animation
    can still be restart-ephemeral in sandbox memory mode, so the maker reads
    the live clearinghouse view atomically.  Strict mode and every non-bot user
    retain the existing durable read path.
    """
    service = get_contract_service(request)
    market = await service.get_market(session, symbol)
    normalized_asset = str(margin_asset or market.margin_asset or market.quote_asset).upper()
    if (settings.persistence_mode == "memory" and str(user.role) == ROLE_BOT):
        runtime = request.app.state.runtime
        async with runtime.clearinghouse.global_lock:
            account = service._fast_contract_account_payload(int(user.id), normalized_asset)
            position = runtime.clearinghouse.position_snapshot(int(user.id), int(market.id))
            positions: list[dict] = []
            if position is not None and Decimal(position.quantity) > 0 and str(position.side) in ("long", "short"):
                mark_price = Decimal(position.mark_price) if position.mark_price is not None else service.mark_price(market)
                positions.append(
                    {
                        "symbol": market.symbol,
                        "product_type": market.product_type,
                        "side": str(position.side),
                        "quantity": str(Decimal(position.quantity)),
                        "entry_price": str(Decimal(position.entry_price)),
                        "mark_price": str(mark_price),
                        "leverage": str(Decimal(position.leverage)),
                        "isolated_margin": str(Decimal(position.isolated_margin)),
                        "maintenance_margin": str(Decimal(position.maintenance_margin)),
                        "unrealized_pnl": str(position.unrealized(mark_price)),
                        "realized_pnl": str(Decimal(position.realized_pnl)),
                    }
                )
        return {
            "symbol": market.symbol,
            "state_source": "clearinghouse",
            "account": account,
            "positions": positions,
        }

    account = await service.serialize_account(session, user.id, normalized_asset)
    rows = await session.execute(
        select(ContractPosition).where(
            ContractPosition.user_id == user.id,
            ContractPosition.market_id == market.id,
            *ContractPosition.active_filters(),
        )
    )
    return {
        "symbol": market.symbol,
        "state_source": "database",
        "account": account,
        "positions": [await service.serialize_position(position, market, session) for position in rows.scalars()],
    }


@router.get("/contracts/positions")
async def get_contract_positions(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(ContractPosition, Market)
        .join(Market, Market.id == ContractPosition.market_id)
        .where(ContractPosition.user_id == user.id, Market.product_type == PRODUCT_TYPE_PERP, *ContractPosition.active_filters())
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Market.symbol.asc()))
    return {"items": [await service.serialize_position(position, market, session) for position, market in rows.all()]}


@router.get("/contracts/settings/{symbol}")
async def get_contract_setting(
    symbol: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    market = await service.get_market(session, symbol)
    setting = await service.get_setting(session, user.id, market)
    return await service.serialize_setting(setting, market)


@router.put("/contracts/settings/{symbol}")
async def update_contract_setting(
    symbol: str,
    payload: ContractLeverageUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    try:
        market = await service.get_market(session, symbol)
        setting = await service.update_setting(
            session,
            user,
            market,
            leverage=payload.leverage,
            margin_mode=payload.margin_mode,
            position_mode=payload.position_mode,
        )
        return await service.serialize_setting(setting, market)
    except ContractValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _place_contract_order_durable(
    request: Request,
    session: AsyncSession,
    user: User,
    payload,
) -> dict:
    """平台契约下的 slow durable 合约下单（含平仓一致性保护）。

    平仓（reduce_only/close）前先排空 write-behind 并等待耐用仓位与快速
    镜像收敛，避免重放回填把已平仓位重新挂起。
    """
    service = get_contract_service(request)
    if str(payload.position_action or "open") == "close" or bool(payload.reduce_only):
        await drain_writer(request)
        market = await service.get_market(session, payload.symbol)
        try:
            await wait_contract_position_converged(request, session, user, market, timeout_seconds=10.0)
        except ContractValidationError:
            await session.rollback()
    return await service.place_order(session, user, payload)


@router.post("/contracts/orders")
async def create_contract_order(
    payload: ContractOrderCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="PERP_PLACE",
        payload=payload,
        symbol=getattr(payload, "symbol", None),
        product_type="PERP",
        account_domain="PERP_MARGIN",
        client_order_id=getattr(payload, "client_order_id", None),
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        with causal_command_context(envelope):
            if user.role == ROLE_BOT:
                fast_result = await service.place_order_fast(session, user, payload)
                if fast_result is not None:
                    result = fast_result
                else:
                    raise HTTPException(status_code=503, detail="fast order path unavailable")
            else:
                result = await _place_contract_order_durable(request, session, user, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except SyntheticFlowUnavailableError as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="SYNTHETIC_FLOW_UNAVAILABLE", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ContractValidationError as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="CONTRACT_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp place exception: {exc.detail}")
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp place exception: {exc}")
        raise


@router.post("/contracts/quote-set")
async def replace_contract_quote_set(
    payload: QuoteSetReplaceRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_quote_set_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="QUOTE_SET_REPLACE",
        payload=payload,
        symbol=payload.symbol,
        product_type="PERP",
        account_domain="PERP_MARGIN",
        strategy_instance=payload.strategy_instance,
        generation=payload.generation,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if (user.role == ROLE_BOT) and not service.contract_service._fast_writer_ready():
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        with causal_command_context(envelope):
            result = await service.submit_contract(session, user, payload)
        return matching_ack_response(await complete_causal_command(request, envelope, result, response=result))
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except (ContractValidationError, OrderValidationError, ValueError) as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="QUOTE_SET_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp quote set exception: {exc.detail}")
        raise
    except Exception as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp quote set exception: {exc}")
        raise


@router.post("/contracts/orders/batch")
async def create_contract_order_batch(
    payload: ContractOrderBatchCreateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="PERP_PLACE_BATCH",
        payload=payload,
        product_type="PERP",
        account_domain="PERP_MARGIN",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            if not service._fast_writer_ready():
                raise HTTPException(status_code=503, detail="fast order path unavailable")
            fast_result = await service.place_order_batch_fast(session, user, payload.orders)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.place_order_batch(session, user, payload.orders)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except ContractValidationError as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="PERP_BATCH_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp batch exception: {exc.detail}")
        raise


@router.patch("/contracts/orders/{order_id}")
async def amend_contract_order(
    order_id: str,
    payload: ContractOrderAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="PERP_AMEND",
        payload={
            "order_id": order_id,
            **(
                payload.model_dump(mode="python")
                if hasattr(payload, "model_dump")
                else dict(vars(payload))
            ),
        },
        product_type="PERP",
        account_domain="PERP_MARGIN",
        client_order_id=order_id,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            fast_result = await service.amend_order_fast(session, user, order_id, payload)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.amend_order(session, user, order_id, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except ContractValidationError as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="PERP_AMEND_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp amend exception: {exc.detail}")
        raise


@router.post("/contracts/orders/amend-batch")
async def amend_contract_order_batch(
    payload: ContractOrderBatchAmendRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="PERP_AMEND_BATCH",
        payload=payload,
        symbol=payload.symbol,
        product_type="PERP",
        account_domain="PERP_MARGIN",
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            if not service._fast_writer_ready():
                raise HTTPException(status_code=503, detail="fast order path unavailable")
            fast_result = await service.amend_order_batch_fast(session, user, payload)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.amend_order_batch(session, user, payload)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except ContractValidationError as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="PERP_AMEND_BATCH_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp amend batch exception: {exc.detail}")
        raise


@router.delete("/contracts/orders/{order_id}")
async def cancel_contract_order(
    order_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    envelope, receipt, is_new = await begin_causal_command(
        request,
        user=user,
        command_type="PERP_CANCEL",
        payload={"order_id": order_id},
        product_type="PERP",
        account_domain="PERP_MARGIN",
        client_order_id=order_id,
    )
    if not is_new:
        stored = existing_response_or_raise(receipt)
        if stored is not None:
            return stored
        return {"causal": receipt.as_dict()}
    try:
        if user.role == ROLE_BOT:
            fast_result = await service.cancel_order_fast(session, user, order_id)
            if fast_result is not None:
                return await complete_causal_command(request, envelope, fast_result, response=fast_result)
            raise HTTPException(status_code=503, detail="fast order path unavailable")
        result = await service.cancel_order(session, user, order_id)
        return await complete_causal_command(request, envelope, result, response=result)
    except NotExecuted as exc:
        await session.rollback()
        await reject_causal_command(request, envelope, code="MATCHING_NOT_EXECUTED", stage="MATCHING_ADMISSION", reason=str(exc))
        raise HTTPException(status_code=503, detail={"status": "NOT_EXECUTED", "reason": str(exc)}) from exc
    except ContractValidationError as exc:
        if request.app.state.runtime.engine.fault.halted:
            await session.rollback()
            await mark_unknown_causal(request, envelope, reason=str(exc))
            raise HTTPException(status_code=503, detail={"status": "UNKNOWN", "reason": str(exc)}) from exc
        await session.rollback()
        await reject_causal_command(request, envelope, code="PERP_CANCEL_REJECTED", stage="RISK", reason=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException as exc:
        await session.rollback()
        await mark_unknown_causal(request, envelope, reason=f"perp cancel exception: {exc.detail}")
        raise


@router.get("/admin/contracts/orders/metrics")
async def admin_contract_order_metrics(
    request: Request,
    _admin: User = Depends(get_admin_user),
):
    service = get_contract_service(request)
    metrics = service.fast_metrics_snapshot()
    writer = getattr(service.runtime, "persistence_writer", None)
    if writer is not None:
        metrics["persistence_writer"] = writer.metrics_snapshot()
    return metrics


@router.get("/contracts/orders/open")
async def get_open_contract_orders(
    request: Request,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    if (settings.persistence_mode == "memory" and str(user.role) == ROLE_BOT):
        normalized_symbol = str(symbol or "").upper()
        async with service.runtime.clearinghouse.global_lock:
            snapshots = [
                dict(snap)
                for snap in service._fast_contract_orders.values()
                if int(snap.get("user_id") or -1) == int(user.id)
                and str(snap.get("product_type") or "") == PRODUCT_TYPE_PERP
                and str(snap.get("status") or "") in {ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED}
                and Decimal(snap.get("remaining_quantity") or 0) > 0
                and (not normalized_symbol or str(snap.get("symbol") or "").upper() == normalized_symbol)
            ]
        market_ids = {int(snap.get("market_id") or 0) for snap in snapshots if int(snap.get("market_id") or 0) > 0}
        markets = {}
        if market_ids:
            rows = await session.execute(select(Market).where(Market.id.in_(market_ids)))
            markets = {int(market.id): market for market in rows.scalars()}
        items = [
            service._fast_serialize_order(markets[int(snap["market_id"])], snap)
            for snap in snapshots
            if int(snap.get("market_id") or 0) in markets
        ]
        items.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
        return {"items": items, "state_source": "fast_mirror"}
    if symbol:
        market = await session.scalar(
            select(Market).where(Market.symbol == symbol.upper(), Market.product_type == PRODUCT_TYPE_PERP)
        )
        if market is None:
            return {"items": []}
        rows = await session.execute(
            select(Order)
            .where(
                Order.user_id == user.id,
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            )
        )
        orders = sorted(rows.scalars(), key=lambda order: order.created_at, reverse=True)
        return {"items": [await service.serialize_order(session, order, market.symbol, market=market) for order in orders]}
    stmt = (
        select(Order, Market.symbol)
        .join(Market, Market.id == Order.market_id)
        .where(
            Order.user_id == user.id,
            Order.product_type == PRODUCT_TYPE_PERP,
            Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
        )
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Order.created_at.desc()))
    return {"items": [await service.serialize_order(session, order, market_symbol) for order, market_symbol in rows.all()]}


@router.get("/contracts/orders/history")
async def get_contract_order_history(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(Order, Market.symbol)
        .join(Market, Market.id == Order.market_id)
        .where(Order.user_id == user.id, Order.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
    return {"items": [await service.serialize_order(session, order, market_symbol) for order, market_symbol in rows.all()]}


@router.get("/contracts/trades")
async def get_contract_trades(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(Trade, Market)
        .join(Market, Market.id == Trade.market_id)
        .where(
            Trade.product_type == PRODUCT_TYPE_PERP,
            or_(Trade.taker_user_id == user.id, Trade.maker_user_id == user.id),
        )
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Trade.executed_at.desc()).limit(limit))
    return {
        "items": [
            await service.serialize_account_trade(trade, market.symbol, user.id, market)
            for trade, market in rows.all()
        ]
    }


@router.get("/contracts/funding/events")
async def get_contract_funding_events(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    price_service = get_contract_price_service(request)
    stmt = (
        select(ContractFundingEvent, Market)
        .join(Market, Market.id == ContractFundingEvent.market_id)
        .where(ContractFundingEvent.user_id == user.id, Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(ContractFundingEvent.funding_time.desc()).limit(limit))
    return {"items": [price_service.serialize_funding_event(event, market) for event, market in rows.all()]}


@router.get("/contracts/ledger")
async def get_contract_ledger(
    request: Request,
    symbol: str | None = None,
    margin_asset: str = "USDT",
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    asset = margin_asset.upper().strip() or "USDT"
    stmt = (
        select(ContractLedgerEntry, Market.symbol)
        .outerjoin(Market, Market.id == ContractLedgerEntry.market_id)
        .where(
            ContractLedgerEntry.user_id == user.id,
            ContractLedgerEntry.margin_asset == asset,
        )
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(ContractLedgerEntry.created_at.desc()).limit(limit))
    return {"items": [serialize_contract_ledger_entry(entry, market_symbol) for entry, market_symbol in rows.all()]}


@router.get("/contracts/liquidations")
async def get_contract_liquidations(
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(ContractLiquidationEvent, Market)
        .join(Market, Market.id == ContractLiquidationEvent.market_id)
        .where(ContractLiquidationEvent.user_id == user.id, Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(ContractLiquidationEvent.liquidated_at.desc()).limit(limit))
    return {"items": [service.serialize_liquidation_event(event, market) for event, market in rows.all()]}


@router.get("/admin/contracts/accounts")
async def admin_contract_accounts(
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    rows = await session.execute(
        select(User, ContractAccount)
        .join(ContractAccount, ContractAccount.user_id == User.id)
        .where(User.role != "admin")
        .order_by(User.id.asc(), ContractAccount.margin_asset.asc())
    )
    items = []
    for user, account in rows.all():
        items.append({"user": {"id": user.id, "username": user.username, "role": user.role}, "account": await service.serialize_existing_account(session, account)})
    await session.commit()
    return {"items": items}


@router.post("/admin/contracts/accounts/adjust")
async def admin_adjust_contract_account(
    payload: ContractAccountAdjustRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to adjust contract account")
    service = get_contract_service(request)
    try:
        account = await service.adjust_account(
            session,
            user_id=payload.user_id,
            margin_asset=payload.margin_asset,
            amount=payload.amount,
        )
        response = {"ok": True, "account": await service.serialize_account(session, account.user_id, account.margin_asset)}
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="adjust_contract_account",
            target_type="user",
            target_id=account.user_id,
            status="success",
            summary=f"调整用户 {account.user_id} 合约保证金账户 {payload.amount} {account.margin_asset}",
            result={
                "user_id": account.user_id,
                "margin_asset": account.margin_asset,
                "amount": str(payload.amount),
                "reason": payload.reason,
                "wallet_balance": str(account.wallet_balance),
                "available_margin": str(account.available_margin),
            },
        )
        await session.commit()
        return response
    except ContractValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/positions")
async def admin_contract_positions(
    request: Request,
    symbol: str | None = None,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(ContractPosition, Market, User)
        .join(Market, Market.id == ContractPosition.market_id)
        .join(User, User.id == ContractPosition.user_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP, *ContractPosition.active_filters())
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Market.symbol.asc(), User.id.asc()))
    return {
        "items": [
            {
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "position": await service.serialize_position(position, market, session),
            }
            for position, market, user in rows.all()
        ]
    }


@router.get("/admin/contracts/orders")
async def admin_contract_orders(
    request: Request,
    symbol: str | None = None,
    user_id: int | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(Order, Market, User)
        .join(Market, Market.id == Order.market_id)
        .join(User, User.id == Order.user_id)
        .where(Order.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(Order.user_id == user_id)
    normalized_status = (status or "").strip().lower()
    if normalized_status in {"open", "live"}:
        stmt = stmt.where(Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]))
    elif normalized_status and normalized_status != "all":
        stmt = stmt.where(Order.status == normalized_status)
    rows = await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit))
    return {
        "items": [
            {
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "order": await service.serialize_order(session, order, market.symbol, market=market),
            }
            for order, market, user in rows.all()
        ]
    }


@router.get("/admin/contracts/trades")
async def admin_contract_trades(
    request: Request,
    symbol: str | None = None,
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(Trade, Market)
        .join(Market, Market.id == Trade.market_id)
        .where(Trade.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(or_(Trade.taker_user_id == user_id, Trade.maker_user_id == user_id))
    rows = await session.execute(stmt.order_by(Trade.executed_at.desc()).limit(limit))
    return {"items": [await service.serialize_trade(trade, market.symbol, market) for trade, market in rows.all()]}


@router.get("/admin/contracts/market-states")
async def admin_contract_market_states(
    request: Request,
    symbol: str | None = None,
    refresh_external: bool = Query(default=True),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    price_service = get_contract_price_service(request)
    stmt = select(Market).where(Market.product_type == PRODUCT_TYPE_PERP)
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Market.symbol.asc()))
    items = []
    for market in rows.scalars():
        items.append(await price_service.serialize_market_state(session, market, fetch_external=refresh_external, persist=False))
    return {"items": items}


@router.get("/admin/contracts/maintenance")
async def admin_contract_maintenance_status(
    request: Request,
    _: User = Depends(get_admin_user),
):
    maintenance_service = get_contract_maintenance_service(request)
    liquidity_service = get_contract_liquidity_service(request)
    status = maintenance_service.serialize_runtime_status()
    status["liquidity"] = liquidity_service.serialize_runtime_status()
    return status


@router.post("/admin/contracts/market-states/{symbol}/refresh")
async def admin_refresh_contract_market_state(
    symbol: str,
    request: Request,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    contract_service = get_contract_service(request)
    price_service = get_contract_price_service(request)
    try:
        market = await contract_service.get_market(session, symbol)
        state = await price_service.serialize_market_state(session, market, fetch_external=True)
        await session.commit()
        return {"ok": True, "state": state}
    except (ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/contracts/funding/{symbol}/settle")
async def admin_settle_contract_funding(
    symbol: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to settle funding")
    contract_service = get_contract_service(request)
    price_service = get_contract_price_service(request)
    try:
        market = await contract_service.get_market(session, symbol)
        response = await price_service.settle_funding(session, market, fetch_external=True)
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="settle_funding",
            target_type="market",
            target_id=market.id,
            target_symbol=market.symbol,
            status="success",
            summary=f"手动结算 {market.symbol} 资金费",
            result=response,
        )
        await session.commit()
        return response
    except (ContractValidationError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/funding/events")
async def admin_contract_funding_events(
    request: Request,
    symbol: str | None = None,
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    price_service = get_contract_price_service(request)
    stmt = (
        select(ContractFundingEvent, Market, User)
        .join(Market, Market.id == ContractFundingEvent.market_id)
        .join(User, User.id == ContractFundingEvent.user_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(ContractFundingEvent.user_id == user_id)
    rows = await session.execute(stmt.order_by(ContractFundingEvent.funding_time.desc()).limit(limit))
    return {
        "items": [
            {
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "event": price_service.serialize_funding_event(event, market),
            }
            for event, market, user in rows.all()
        ]
    }


@router.get("/admin/contracts/ledger")
async def admin_contract_ledger(
    request: Request,
    user_id: int | None = None,
    symbol: str | None = None,
    margin_asset: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    stmt = (
        select(ContractLedgerEntry, Market.symbol, User)
        .outerjoin(Market, Market.id == ContractLedgerEntry.market_id)
        .join(User, User.id == ContractLedgerEntry.user_id)
    )
    if user_id is not None:
        stmt = stmt.where(ContractLedgerEntry.user_id == user_id)
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if margin_asset:
        stmt = stmt.where(ContractLedgerEntry.margin_asset == margin_asset.upper().strip())
    rows = await session.execute(stmt.order_by(ContractLedgerEntry.created_at.desc()).limit(limit))
    return {
        "items": [
            {
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "entry": serialize_contract_ledger_entry(entry, market_symbol),
            }
            for entry, market_symbol, user in rows.all()
        ]
    }


@router.get("/admin/contracts/funding/settlements")
async def admin_contract_funding_settlements(
    request: Request,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    price_service = get_contract_price_service(request)
    stmt = (
        select(ContractFundingSettlement, Market)
        .join(Market, Market.id == ContractFundingSettlement.market_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    normalized_status = (status or "").strip().lower()
    if normalized_status == "active":
        stmt = stmt.where(ContractFundingSettlement.status.notin_(["settled", "failed"]))
    elif normalized_status and normalized_status != "all":
        stmt = stmt.where(ContractFundingSettlement.status == normalized_status)
    rows = await session.execute(stmt.order_by(ContractFundingSettlement.funding_time.desc()).limit(limit))
    return {"items": [price_service.serialize_funding_settlement(settlement, market) for settlement, market in rows.all()]}


@router.get("/admin/contracts/funding/jobs")
async def admin_contract_funding_jobs(
    request: Request,
    symbol: str | None = None,
    status: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    maintenance_service = get_contract_maintenance_service(request)
    stmt = (
        select(ContractFundingJob, Market)
        .join(Market, Market.id == ContractFundingJob.market_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    normalized_status = (status or "").strip().lower()
    if normalized_status == "active":
        stmt = stmt.where(ContractFundingJob.status.notin_(["completed", "succeeded", "settled"]))
    elif normalized_status and normalized_status != "all":
        stmt = stmt.where(ContractFundingJob.status == normalized_status)
    rows = await session.execute(stmt.order_by(ContractFundingJob.funding_time.desc()).limit(limit))
    return {"items": [maintenance_service.serialize_funding_job(job, market) for job, market in rows.all()]}


@router.post("/admin/contracts/funding/jobs/retry-failed")
async def retry_failed_contract_funding_jobs(
    payload: ConfirmExecuteRequest,
    request: Request,
    symbol: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to retry failed funding jobs")
    maintenance_service = get_contract_maintenance_service(request)
    response = await maintenance_service.retry_failed_funding_jobs(
        session,
        symbol=symbol,
        limit=limit,
        fetch_external=True,
    )
    await record_admin_operation(
        session,
        actor=admin_user,
        request=request,
        domain="contract",
        operation_type="retry_failed_funding_jobs",
        target_type="funding_jobs",
        target_symbol=symbol.upper() if symbol else None,
        status="success",
        summary=f"批量重试资金费失败任务 {symbol.upper() if symbol else 'ALL'}",
        result=response,
    )
    await session.commit()
    return response


@router.post("/admin/contracts/funding/jobs/{job_id}/retry")
async def retry_contract_funding_job(
    job_id: str,
    payload: ConfirmExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to retry funding job")
    maintenance_service = get_contract_maintenance_service(request)
    try:
        response = await maintenance_service.retry_funding_job(session, job_id, fetch_external=True)
        job = response.get("job") if isinstance(response, dict) else None
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="retry_funding_job",
            target_type="funding_job",
            target_id=job_id,
            target_symbol=job.get("symbol") if isinstance(job, dict) else None,
            status="success",
            summary=f"重试资金费任务 {job_id}",
            result=response,
        )
        await session.commit()
        return response
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/liquidations")
async def admin_contract_liquidations(
    request: Request,
    symbol: str | None = None,
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = (
        select(ContractLiquidationEvent, Market, User)
        .join(Market, Market.id == ContractLiquidationEvent.market_id)
        .join(User, User.id == ContractLiquidationEvent.user_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(ContractLiquidationEvent.user_id == user_id)
    rows = await session.execute(stmt.order_by(ContractLiquidationEvent.liquidated_at.desc()).limit(limit))
    return {
        "items": [
            {
                "user": {"id": user.id, "username": user.username, "role": user.role},
                "event": service.serialize_liquidation_event(event, market),
            }
            for event, market, user in rows.all()
        ]
    }


@router.get("/admin/contracts/liquidations/{event_id}/adl-candidates")
async def admin_contract_adl_candidates(
    event_id: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    row = await session.execute(
        select(ContractLiquidationEvent, Market)
        .join(Market, Market.id == ContractLiquidationEvent.market_id)
        .where(ContractLiquidationEvent.event_id == event_id, Market.product_type == PRODUCT_TYPE_PERP)
    )
    item = row.first()
    if item is None:
        raise HTTPException(status_code=404, detail="liquidation event not found")
    event, market = item
    candidates = await build_contract_adl_candidates(session, service, event, market, limit=limit)
    return {
        "liquidation": service.serialize_liquidation_event(event, market),
        "items": [serialize_adl_candidate(candidate, market) for candidate in candidates],
    }


@router.post("/admin/contracts/liquidations/{event_id}/adl-execute")
async def admin_execute_contract_adl(
    event_id: str,
    payload: ContractAdlExecuteRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to execute ADL")
    service = get_contract_service(request)
    row = await session.execute(
        select(ContractLiquidationEvent, Market)
        .join(Market, Market.id == ContractLiquidationEvent.market_id)
        .where(ContractLiquidationEvent.event_id == event_id, Market.product_type == PRODUCT_TYPE_PERP)
    )
    item = row.first()
    if item is None:
        raise HTTPException(status_code=404, detail="liquidation event not found")
    event, market = item
    try:
        result = await execute_contract_adl(
            session,
            service,
            event,
            market,
            max_candidates=payload.max_candidates,
            now=datetime.now(tz=UTC),
        )
        response = {
            "ok": True,
            "liquidation": service.serialize_liquidation_event(event, market),
            "events": [serialize_adl_event(adl_event, market.symbol) for adl_event in result["events"]],
            "covered_amount": result["covered_amount"],
            "residual_after": result["residual_after"],
            "status": result["status"],
        }
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="execute_adl",
            target_type="liquidation_event",
            target_id=event.event_id,
            target_symbol=market.symbol,
            status="success",
            summary=f"执行 {market.symbol} 强平事件 {event.event_id} ADL",
            result={
                "covered_amount": response["covered_amount"],
                "residual_after": response["residual_after"],
                "status": response["status"],
                "adl_event_count": len(response["events"]),
            },
        )
        await session.commit()
        return response
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/adl-events")
async def admin_contract_adl_events(
    symbol: str | None = None,
    user_id: int | None = None,
    liquidation_event_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    stmt = (
        select(ContractAdlEvent, Market, User)
        .join(Market, Market.id == ContractAdlEvent.market_id)
        .join(User, User.id == ContractAdlEvent.user_id)
        .where(Market.product_type == PRODUCT_TYPE_PERP)
    )
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(ContractAdlEvent.user_id == user_id)
    if liquidation_event_id:
        stmt = stmt.where(ContractAdlEvent.liquidation_event_id == liquidation_event_id)
    rows = await session.execute(stmt.order_by(ContractAdlEvent.created_at.desc()).limit(limit))
    return {
        "items": [
            serialize_adl_event(event, market.symbol, user.username)
            for event, market, user in rows.all()
        ]
    }


@router.get("/admin/contracts/insurance-funds")
async def admin_contract_insurance_funds(
    margin_asset: str | None = None,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    asset = margin_asset.upper().strip() if margin_asset else None
    if asset:
        fund = await get_contract_insurance_fund(session, asset)
        await session.commit()
        return {"items": [serialize_contract_insurance_fund(fund)]}
    await get_contract_insurance_fund(session, "USDT")
    rows = await session.execute(select(ContractInsuranceFund).order_by(ContractInsuranceFund.margin_asset.asc()))
    await session.commit()
    return {"items": [serialize_contract_insurance_fund(fund) for fund in rows.scalars()]}


@router.post("/admin/contracts/insurance-funds/adjust")
async def admin_adjust_contract_insurance_fund(
    payload: ContractInsuranceFundAdjustRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to adjust insurance fund")
    try:
        event = await apply_contract_insurance_change(
            session,
            margin_asset=payload.margin_asset,
            event_type="admin_adjustment",
            amount=payload.amount,
            note=payload.reason,
            created_at=datetime.now(tz=UTC),
        )
        fund = await get_contract_insurance_fund(session, payload.margin_asset)
        response = {
            "ok": True,
            "fund": serialize_contract_insurance_fund(fund),
            "event": serialize_contract_insurance_event(event),
        }
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="adjust_insurance_fund",
            target_type="insurance_fund",
            target_id=payload.margin_asset,
            status="success",
            summary=f"调整 {payload.margin_asset} 合约保险基金 {payload.amount}",
            result=response,
        )
        await session.commit()
        return response
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/insurance-events")
async def admin_contract_insurance_events(
    margin_asset: str | None = None,
    symbol: str | None = None,
    user_id: int | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    stmt = select(ContractInsuranceEvent, Market).outerjoin(Market, Market.id == ContractInsuranceEvent.market_id)
    if margin_asset:
        stmt = stmt.where(ContractInsuranceEvent.margin_asset == margin_asset.upper().strip())
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    if user_id is not None:
        stmt = stmt.where(ContractInsuranceEvent.user_id == user_id)
    rows = await session.execute(stmt.order_by(ContractInsuranceEvent.created_at.desc()).limit(limit))
    return {
        "items": [
            serialize_contract_insurance_event(event, market.symbol if market else None)
            for event, market in rows.all()
        ]
    }


@router.get("/contracts/risk-tiers/{symbol}")
async def get_contract_risk_tiers(
    symbol: str,
    request: Request,
    _: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    try:
        market = await service.get_market(session, symbol)
        tiers = await service.risk_tiers_for_market(session, market)
        if not tiers:
            return {"items": [service.serialize_risk_tier(service.fallback_risk_tier(market), market)]}
        return {"items": [service.serialize_risk_tier(tier, market) for tier in tiers]}
    except ContractValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/admin/contracts/risk-tiers")
async def admin_contract_risk_tiers(
    request: Request,
    symbol: str | None = None,
    _: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    service = get_contract_service(request)
    stmt = select(Market).where(Market.product_type == PRODUCT_TYPE_PERP)
    if symbol:
        stmt = stmt.where(Market.symbol == symbol.upper())
    rows = await session.execute(stmt.order_by(Market.symbol.asc()))
    items = []
    for market in rows.scalars():
        tiers = await service.risk_tiers_for_market(session, market)
        if not tiers:
            items.append(service.serialize_risk_tier(service.fallback_risk_tier(market), market))
        else:
            items.extend(service.serialize_risk_tier(tier, market) for tier in tiers)
    return {"items": items}


@router.put("/admin/contracts/risk-tiers/{symbol}")
async def admin_update_contract_risk_tiers(
    symbol: str,
    payload: ContractRiskLimitTierUpdateRequest,
    request: Request,
    admin_user: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db_session),
):
    if not payload.confirm_execute:
        raise HTTPException(status_code=400, detail="confirm_execute is required to update risk tiers")
    service = get_contract_service(request)
    try:
        market = await service.get_market(session, symbol)
        await session.execute(
            ContractRiskLimitTier.__table__.delete().where(ContractRiskLimitTier.market_id == market.id)
        )
        items = []
        for tier_payload in payload.tiers:
            tier = ContractRiskLimitTier(
                market_id=market.id,
                tier=tier_payload.tier,
                notional_floor=tier_payload.notional_floor,
                notional_cap=tier_payload.notional_cap,
                max_leverage=tier_payload.max_leverage,
                maintenance_margin_rate=tier_payload.maintenance_margin_rate,
                maintenance_amount=tier_payload.maintenance_amount,
            )
            session.add(tier)
            items.append(tier)
        first = items[0]
        market.max_leverage = max(Decimal(item.max_leverage) for item in items)
        market.maintenance_margin_rate = first.maintenance_margin_rate
        if Decimal(market.default_leverage) > Decimal(market.max_leverage):
            market.default_leverage = market.max_leverage
        response = {"ok": True, "items": [service.serialize_risk_tier(tier, market) for tier in items]}
        await record_admin_operation(
            session,
            actor=admin_user,
            request=request,
            domain="contract",
            operation_type="update_risk_tiers",
            target_type="market",
            target_id=market.id,
            target_symbol=market.symbol,
            status="success",
            summary=f"更新 {market.symbol} 合约风险限额阶梯",
            result={"tier_count": len(items), "items": response["items"]},
        )
        await session.commit()
        return response
    except ContractValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
