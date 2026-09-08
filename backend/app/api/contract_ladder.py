"""Existing admin authentication and durable configuration publication."""
from copy import deepcopy
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_admin_user
from app.services.contract_ladder_service import VersionConflict

router = APIRouter(prefix="/admin/contract-ladder", tags=["合约铺单"], dependencies=[Depends(get_admin_user)])


class DraftRequest(BaseModel):
    draft: dict
    expected_version: int = Field(default=0, ge=0)
    test_amount_usdt: str | float | None = None
    test_amounts: list | None = None
    bbo: dict | None = None


class EnabledRequest(BaseModel):
    enabled: bool
    expected_version: int = Field(ge=0)


class ControlRequest(BaseModel):
    allowed_quote_sides: Literal["BOTH", "BUY", "SELL", "NONE"]
    reason: str = Field(min_length=1,max_length=200)
    expected_control_version: int = Field(ge=0)


def service(request):
    result = getattr(request.app.state, "contract_ladder_service", None)
    if result is None:
        raise HTTPException(503, "铺单服务尚未加载")
    return result


def lookup(request, symbol):
    result = service(request)
    if symbol.upper() not in result.records or result.records[symbol.upper()].get("strategy_key", "CONTRACT_LADDER") != "CONTRACT_LADDER":
        raise HTTPException(404, "合约尚未绑定独立铺单实例")
    return result, symbol.upper()


@router.get("")
async def listing(request: Request):
    svc = service(request)
    return {"items": [svc.read(symbol) for symbol in sorted(svc.records) if svc.records[symbol].get("strategy_key", "CONTRACT_LADDER") == "CONTRACT_LADDER"]}


@router.get("/templates")
async def templates():
    return {"shape_templates": [{"value":"BALANCED","name":"均衡"}, {"value":"OUTER_HEAVY","name":"外侧偏厚"}, {"value":"BELLY","name":"中部偏厚"}],
            "time_templates": [{"id":"ALL_DAY","name":"全天","version":"20260907","scope":"全天币种","schedule":{"timezone":"Asia/Shanghai","weekly":[],"date_overrides":[]}},
                               {"id":"US_REGULAR","name":"美股常规时段","version":"20260907","scope":"仅常规周表，节假日需维护例外","schedule":{"timezone":"America/New_York","weekly":[{"days":[1,2,3,4,5],"start":"09:30","end":"16:00","depth_multiplier":"1.2","spread_multiplier":"1"}],"date_overrides":[]}}]}


@router.get("/{symbol}")
async def read(symbol: str, request: Request):
    svc, symbol = lookup(request, symbol)
    return svc.read(symbol)


@router.get("/{symbol}/runtime")
async def runtime(symbol: str, request: Request):
    svc, symbol = lookup(request, symbol)
    return svc.workers[symbol].status()


@router.post("/{symbol}/preview")
async def preview(symbol: str, body: DraftRequest, request: Request):
    svc, symbol = lookup(request, symbol)
    try:
        amounts = body.test_amounts or ([body.test_amount_usdt] if body.test_amount_usdt is not None else None)
        return svc.preview(symbol, body.draft, amounts, body.bbo)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{symbol}/publish")
async def publish(symbol: str, body: DraftRequest, request: Request, user=Depends(get_admin_user)):
    svc, symbol = lookup(request, symbol)
    try:
        return await svc.publish(symbol, body.draft, body.expected_version, user.id)
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{symbol}/enabled")
async def enabled(symbol: str, body: EnabledRequest, request: Request, user=Depends(get_admin_user)):
    svc, symbol = lookup(request, symbol)
    draft = deepcopy(svc.records[symbol]["config"])
    draft["enabled"] = body.enabled
    try:
        return await svc.publish(symbol, draft, body.expected_version, user.id)
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{symbol}/control")
async def control(symbol: str, body: ControlRequest, request: Request):
    svc, symbol = lookup(request, symbol)
    try:
        return await svc.control(symbol,body.allowed_quote_sides,body.reason,body.expected_control_version)
    except VersionConflict as exc:
        raise HTTPException(409,str(exc)) from exc
