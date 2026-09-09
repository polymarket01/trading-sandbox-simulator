"""Unified maker selection and independent FLOW administration."""
import asyncio
import hmac
import time
from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import httpx

from app.api.deps import get_admin_user, get_db_session
from app.models.market import Market
from app.models.market_strategy_config import MarketStrategyConfig
from app.models.market_bot_account import MarketBotAccount
from app.models.user import User
from app.services.independent_flow_service import FlowConfig
from app.services.strategy_accounts import ACCOUNT_SLOTS, allocate_accounts
from app.services.maker_plugins import INTERNAL_MAKER_STRATEGIES, internal_default, strategy_choices, get_plugin

from app.services.maker_lifecycle import maker_serialized, switch_states

SWITCH_STAGE_LABELS = {"stop_old": "停止旧策略", "drain_old": "确认撤净旧挂单", "save_new": "保存新策略", "start_new": "启动新策略"}

router = APIRouter()


def flow(request):
    return request.app.state.independent_flow


async def internal(request: Request):
    supplied = request.headers.get('X-Flow-Token', '')
    if not hmac.compare_digest(supplied, flow(request).token):
        raise HTTPException(403, 'invalid FLOW worker token')


@router.get('/internal/flow/config', dependencies=[Depends(internal)])
async def flow_worker_config(request: Request):
    flow(request).heartbeat = time.monotonic()
    return flow(request).worker_config()


class Tick(BaseModel):
    version: int
    event_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    side: str
    quote: str


@router.post('/internal/flow/{symbol}/tick', dependencies=[Depends(internal)])
async def flow_worker_tick(symbol: str, body: Tick, request: Request):
    try:
        return await flow(request).tick(symbol.upper(), body.version, body.event_id, body.side, body.quote)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/admin/liquidity/flow', dependencies=[Depends(get_admin_user)])
async def flow_listing(request: Request):
    return flow(request).listing()


class FlowSave(BaseModel):
    config: FlowConfig
    expected_version: int = Field(ge=0)


@router.put('/admin/liquidity/flow/{symbol}', dependencies=[Depends(get_admin_user)])
async def save_flow(symbol: str, body: FlowSave, request: Request):
    try:
        return await flow(request).save(symbol.upper(), body.config.model_dump(), body.expected_version)
    except IntegrityError as exc:
        raise HTTPException(409, 'UID 分配冲突，请刷新后重试') from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get('/admin/liquidity/makers', dependencies=[Depends(get_admin_user)])
async def makers(request: Request, session=Depends(get_db_session)):
    from app.api.admin import build_strategy_runtime_bundle, build_maker_instance_start_readiness
    items = []
    for market in (await session.scalars(select(Market).order_by(Market.symbol))).all():
        bundle = await build_strategy_runtime_bundle(session, request, market)
        selected = bundle['strategy']['strategy_key']
        ladder = getattr(request.app.state, 'contract_ladder_service', None)
        worker = ladder.workers.get(market.symbol) if ladder else None
        items.append({'symbol': market.symbol, 'product_type': market.product_type,
            'strategy_key': selected, 'choices': strategy_choices(market.product_type),
            'parameter_editors': {k: ('schema' if get_plugin(k).manifest.get('parameters') else 'legacy') for k in strategy_choices(market.product_type)},
            'source_exchange': 'binance', 'source_symbol': market.price_source_symbol or market.symbol.removesuffix('-PERP'),
            'readiness': build_maker_instance_start_readiness(market, bundle),
            'ladder': worker.status() if worker else None,
            'account_slots': {k: ACCOUNT_SLOTS[k] for k in (strategy_choices(market.product_type))},
            'maker_accounts': [{'uid': x['uid'], 'username': x['username']} for x in bundle['accounts']['makers']],
            'flow_accounts': [{'uid': x['uid'], 'username': x['username']} for x in bundle['accounts']['flows']]})
    return {'items': items, 'switch_supported': True, 'switches': switch_states(request.app.state.runtime)}


class SourceTest(BaseModel):
    exchange: str = 'binance'
    symbol: str = Field(pattern=r'^[\p{L}\p{N}]{2,30}$')


@router.post('/admin/liquidity/{symbol}/source-test', dependencies=[Depends(get_admin_user)])
async def source_test(symbol: str, body: SourceTest, session=Depends(get_db_session)):
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(404, '币对不存在')
    if body.exchange.lower() != 'binance':
        raise HTTPException(422, '当前策略仅实现 Binance 上游适配，不提供无效交易所选项')
    url = 'https://fapi.binance.com/fapi/v1/ticker/bookTicker' if market.product_type == 'PERP' else 'https://api.binance.com/api/v3/ticker/bookTicker'
    from decimal import Decimal
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            result = await client.get(url, params={'symbol': body.symbol.upper()})
            result.raise_for_status()
            data = result.json()
            rules = {}
            rules_error = None
            try:
                info_url = 'https://fapi.binance.com/fapi/v1/exchangeInfo' if market.product_type == 'PERP' else 'https://api.binance.com/api/v3/exchangeInfo'
                info = await client.get(info_url, params={} if market.product_type == 'PERP' else {'symbol': body.symbol.upper()})
                info.raise_for_status()
                upstream = next(item for item in info.json()['symbols'] if item['symbol'] == body.symbol.upper())
                filters = {item['filterType']: item for item in upstream['filters']}
                price_filter, lot = filters['PRICE_FILTER'], filters['LOT_SIZE']
                from app.core.decimal_utils import decimal_scale
                rules = {'price_tick': price_filter['tickSize'], 'qty_step': lot['stepSize'],
                         'price_precision': decimal_scale(price_filter['tickSize']), 'qty_precision': decimal_scale(lot['stepSize']),
                         'min_qty': lot['minQty'], 'min_notional': filters.get('NOTIONAL', filters.get('MIN_NOTIONAL', {})).get('minNotional', filters.get('MIN_NOTIONAL', {}).get('notional')),
                         'quote_asset': upstream.get('quoteAsset')}
            except Exception as exc:
                rules_error = '交易规则获取失败：' + str(exc)
        if not 0 < Decimal(data['bidPrice']) < Decimal(data['askPrice']) or min(Decimal(data['bidQty']), Decimal(data['askQty'])) <= 0:
            raise ValueError('BBO价格或数量无效')
        return {'ok': True, 'exchange': 'binance', 'symbol': body.symbol.upper(), 'bid': data['bidPrice'], 'ask': data['askPrice'],
                'bid_qty': data['bidQty'], 'ask_qty': data['askQty'], 'latency_ms': round((time.monotonic()-start)*1000), 'transport': 'REST', 'rules': rules, 'rules_error': rules_error}
    except Exception as exc:
        return {'ok': False, 'reason': str(exc), 'symbol': body.symbol.upper()}

class MakerSave(BaseModel):
    uids: list[int | None] | None = None
    strategy_key: str
    source: SourceTest


@router.put('/admin/liquidity/makers/{symbol}', dependencies=[Depends(get_admin_user)])
@maker_serialized
async def save_maker(symbol: str, body: MakerSave, request: Request, user=Depends(get_admin_user), session=Depends(get_db_session)):
    from app.api.admin import current_strategy_config, maker_instance_status
    from app.services.strategy_config_service import set_selected_market_strategy, ensure_strategy_templates
    from app.models.order import Order
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None:
        raise HTTPException(404, '币对不存在')
    choices = strategy_choices(market.product_type)
    if body.strategy_key not in choices or body.source.exchange.lower() != 'binance':
        raise HTTPException(422, '不支持该产品的策略或上游交易所')
    selected, _, _ = await current_strategy_config(session, request, market)
    svc = request.app.state.contract_ladder_service
    worker = svc.workers.get(market.symbol)
    if (worker and (worker.config.get('enabled') or worker.orders or getattr(worker, 'inflight', None) or getattr(worker, 'unknown', None))) or maker_instance_status(market.symbol, request).get('running'):
        raise HTTPException(409, '请先停止当前铺单并等待自有挂单撤净，再更换策略或上游映射')
    open_order = await session.scalar(select(Order.id).join(MarketBotAccount, MarketBotAccount.user_id == Order.user_id).where(
        Order.market_id == market.id, MarketBotAccount.market_id == market.id, MarketBotAccount.role == 'maker',
        Order.status.in_(['new', 'partially_filled'])).limit(1))
    if open_order is not None:
        raise HTTPException(409, '旧 Maker 仍有挂单，请先使用停止并撤单')
    checked = await source_test(symbol, body.source, session)
    if not checked['ok']:
        raise HTTPException(422, checked)
    market.price_source = 'binance'
    market.price_source_symbol = body.source.symbol.upper()
    await ensure_strategy_templates(session)
    assigned = await allocate_accounts(session, market, 'maker', body.strategy_key, body.uids)
    if body.strategy_key in INTERNAL_MAKER_STRATEGIES:
        row = await session.scalar(select(MarketStrategyConfig).where(MarketStrategyConfig.market_id == market.id,
            MarketStrategyConfig.strategy_key == body.strategy_key))
        document = deepcopy(row.config_json) if row and row.config_json.get('config') else {'desired_version': 0, 'config': internal_default(body.strategy_key, market.price_source_symbol)}
        if body.strategy_key == 'SIMPLE_BBO' and not (row and row.config_json.get('config')):
            from app.services.simple_bbo import BBOParameters
            from app.models.strategy_template import StrategyTemplate
            template = await session.scalar(select(StrategyTemplate).where(StrategyTemplate.strategy_key == 'SIMPLE_BBO'))
            document['config'].update(BBOParameters.model_validate(template.default_config_json).model_dump(mode='json'))
        document['config']['enabled'] = False
        document['config']['primary_source']['symbol'] = market.price_source_symbol
        document['desired_version'] += 1
        await set_selected_market_strategy(session, market, body.strategy_key, config_json=document, updated_by_user_id=user.id)
    else:
        await set_selected_market_strategy(session, market, body.strategy_key, updated_by_user_id=user.id)
    await session.commit()
    invalidate = getattr(getattr(svc, 'adapter', None), 'invalidate_identity', None)
    if invalidate:
        invalidate(market.symbol, market.id)
    request.app.state.runtime.set_liquidity_strategy_selection(market.symbol, body.strategy_key)
    await svc.refresh()
    return {'ok': True, 'strategy_key': body.strategy_key, 'source_symbol': market.price_source_symbol, 'uids': assigned}

class MakerAction(BaseModel):
    action: str


class MakerSwitch(MakerSave):
    expected_strategy_key: str


async def stop_for_switch(request, session, market, strategy, user):
    from app.api.admin import (stop_market_maker_instance, cancel_contract_market_bot_open_orders,
                               cancel_market_bot_open_orders)
    from app.schemas.api import ConfirmExecuteRequest
    if strategy in INTERNAL_MAKER_STRATEGIES:
        await maker_control(market.symbol, MakerAction(action='stop'), request, user, session)
        return
    stopped = await stop_market_maker_instance(market.symbol, ConfirmExecuteRequest(confirm_execute=True), request, user, session)
    if stopped.get('instance', {}).get('running') or stopped.get('instance', {}).get('status') != 'stopped':
        raise HTTPException(409, '旧策略进程停止未确认')
    cancel = cancel_contract_market_bot_open_orders if market.product_type == 'PERP' else cancel_market_bot_open_orders
    cleanup = await cancel(request, session, market, maker_only=True)
    await session.commit()
    if not cleanup.get('ok'):
        raise HTTPException(409, '旧 Maker 撤单失败，新策略未启动')


async def wait_old_maker_clear(request, session, market, strategy, *, timeout=15):
    """Confirm both the writer's stop and authoritative owned order removal."""
    from app.api.admin import maker_instance_status
    from app.models.order import Order
    runtime = request.app.state.runtime
    svc = request.app.state.contract_ladder_service
    if strategy in INTERNAL_MAKER_STRATEGIES:
        worker = svc.workers.get(market.symbol)
        if worker is None:
            raise HTTPException(409, '旧内部策略实例缺失，无法确认执行终态')
        deadline = time.monotonic() + timeout
        while True:
            if worker.unknown:
                raise HTTPException(409, '旧策略存在未知执行结果，禁止启动新策略')
            if not worker.config.get('enabled') and not worker.inflight and not worker.orders and worker.cancel_status == 'CONFIRMED':
                if not await svc.adapter.own_orders(market.symbol, worker.uid):
                    break
            if time.monotonic() >= deadline:
                raise HTTPException(409, '等待旧策略停止并撤净超时，新策略未启动')
            await asyncio.sleep(.05)
    elif maker_instance_status(market.symbol, request, configured_strategy_version=strategy).get('running'):
        raise HTTPException(409, '旧策略进程仍存活，新策略未启动')
    maker_uids = set((await session.scalars(select(MarketBotAccount.user_id).where(
        MarketBotAccount.market_id == market.id, MarketBotAccount.role == 'maker'))).all())
    # Internal unfilled orders are not necessarily materialized in SQLite.
    async with runtime.market_locks[market.symbol]:
        book = runtime.engine.ensure_market(market.symbol)
        if any(o.user_id in maker_uids for o in book.orders.values()):
            raise HTTPException(409, '撮合盘口仍有旧 Maker 挂单，新策略未启动')
    if await session.scalar(select(Order.id).where(Order.market_id == market.id, Order.user_id.in_(maker_uids),
                                                  Order.status.in_(['new', 'partially_filled'])).limit(1)) is not None:
        raise HTTPException(409, '订单记录仍有旧 Maker 活跃挂单，新策略未启动')


@router.post('/admin/liquidity/makers/{symbol}/switch')
@maker_serialized
async def switch_maker(symbol: str, body: MakerSwitch, request: Request, user=Depends(get_admin_user), session=Depends(get_db_session)):
    from app.api.admin import current_strategy_config
    from app.services.strategy_accounts import validate_uid
    symbol = symbol.upper()
    market = await session.scalar(select(Market).where(Market.symbol == symbol))
    if market is None:
        raise HTTPException(404, '币对不存在')
    choices = strategy_choices(market.product_type)
    if body.strategy_key not in choices:
        raise HTTPException(422, '目标策略不支持此产品')
    selected, _, _ = await current_strategy_config(session, request, market)
    old_key = selected.strategy_key
    if old_key != body.expected_strategy_key or old_key == body.strategy_key:
        raise HTTPException(409, '当前策略已变化或已是目标策略，请刷新后操作')
    requested = [uid for uid in (body.uids or []) if uid is not None]
    if len(body.uids or []) > ACCOUNT_SLOTS[body.strategy_key] or len(set(requested)) != len(requested):
        raise HTTPException(422, '执行 UID 数量错误或重复')
    for uid in requested:
        if uid <= 0:
            raise HTTPException(422, '执行 UID 必须是正整数')
        await validate_uid(session, uid, market.id, 'maker')
    checked = await source_test(symbol, body.source, session)
    if not checked['ok']:
        raise HTTPException(422, {'message': '目标上游测试未通过，旧策略未停止', 'source_test': checked})
    state = {'status': 'running', 'stage': 'stop_old', 'from_strategy': old_key, 'to_strategy': body.strategy_key,
             'old_stopped': False, 'new_selected': False, 'new_started': False, 'started_at': int(time.time()*1000)}
    market_id, user_id = market.id, user.id
    switch_states(request.app.state.runtime)[symbol] = state
    try:
        await stop_for_switch(request, session, market, old_key, user)
        state['stage'] = 'drain_old'
        await wait_old_maker_clear(request, session, market, old_key)
        state['old_stopped'] = True
        state['stage'] = 'save_new'
        await save_maker(symbol, MakerSave(strategy_key=body.strategy_key, source=body.source, uids=body.uids), request, user, session)
        state['new_selected'] = True
        state['stage'] = 'start_new'
        if body.strategy_key == 'CONTRACT_LADDER':
            # LADDER validates a real source snapshot before enabling. Its feed
            # starts with the stopped worker; give that new connection time to warm.
            worker = request.app.state.contract_ladder_service.workers[symbol]
            deadline = time.monotonic() + 10
            while not worker.quote_source()['can_quote']:
                if time.monotonic() >= deadline:
                    raise HTTPException(409, '新策略上游行情预热超时，保持停止')
                await asyncio.sleep(.05)
        started = await maker_control(symbol, MakerAction(action='start'), request, user, session)
        # Started means enabled/process launched, not a promise of live quotes.
        if body.strategy_key in INTERNAL_MAKER_STRATEGIES:
            confirmed = started.get('config', {}).get('enabled')
        else:
            confirmed = started.get('running')
        if not confirmed:
            raise HTTPException(409, '新策略启动未确认')
        state['new_started'] = True
        state.update(status='complete', stage='complete', finished_at=int(time.time()*1000))
        return {'ok': True, 'transition': dict(state), 'instance': started,
                'message': '旧策略已停止并撤净，新策略已启用；实际报价以行情和运行状态为准'}
    except asyncio.CancelledError:
        state.update(status='interrupted', error='切换请求中断，请核对当前策略与挂单；未自动重试')
        if state['new_selected'] and state['stage'] == 'start_new':
            try:
                await session.rollback()
                market = await session.get(Market, market_id)
                user = await session.get(User, user_id)
                await stop_for_switch(request, session, market, body.strategy_key, user)
                await wait_old_maker_clear(request, session, market, body.strategy_key)
                state['new_cleanup'] = 'confirmed'
            except BaseException as cleanup_exc:
                state['new_cleanup'] = 'unconfirmed'
                state['cleanup_error'] = str(cleanup_exc)
        raise
    except Exception as exc:
        await session.rollback()
        market = await session.get(Market, market_id)
        user = await session.get(User, user_id)
        error = exc.detail if isinstance(exc, HTTPException) else str(exc)
        # Start may have partially succeeded. Stop the selected new writer rather
        # than reactivating the old one or claiming a request error means no orders.
        if state['new_selected'] and state['stage'] == 'start_new':
            try:
                await stop_for_switch(request, session, market, body.strategy_key, user)
                await wait_old_maker_clear(request, session, market, body.strategy_key)
                state['new_cleanup'] = 'confirmed'
            except Exception as cleanup_exc:
                state['new_cleanup'] = 'unconfirmed'
                state['cleanup_error'] = str(cleanup_exc)
        state.update(status='failed', error=str(error), finished_at=int(time.time()*1000))
        raise HTTPException(409, {'message': f"切换停在 {SWITCH_STAGE_LABELS.get(state['stage'], state['stage'])}：{error}；未自动恢复旧策略，请刷新检查", 'transition': dict(state)}) from exc


@router.get('/admin/liquidity/simple-bbo/{symbol}', dependencies=[Depends(get_admin_user)])
async def read_simple_bbo(symbol: str, request: Request):
    svc = request.app.state.contract_ladder_service
    record = svc.records.get(symbol.upper())
    if not record or record.get('strategy_key') != 'SIMPLE_BBO':
        raise HTTPException(404, '请先保存极简四档铺单策略选择')
    return svc.read(symbol.upper())


class BBOSave(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    second_level_notional: str
    second_level_distance_bps: str


@router.put('/admin/liquidity/simple-bbo/{symbol}')
@maker_serialized
async def save_simple_bbo(symbol: str, body: BBOSave, request: Request, user=Depends(get_admin_user)):
    record = await read_simple_bbo(symbol, request)
    draft = deepcopy(record['config'])
    draft.update(second_level_notional=body.second_level_notional, second_level_distance_bps=body.second_level_distance_bps)
    from app.services.contract_ladder_service import VersionConflict
    try:
        return await request.app.state.contract_ladder_service.publish(symbol.upper(), draft, body.expected_version, user.id)
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post('/admin/liquidity/makers/{symbol}/control')
@maker_serialized
async def maker_control(symbol: str, body: MakerAction, request: Request, user=Depends(get_admin_user), session=Depends(get_db_session)):
    from app.api.admin import current_strategy_config, start_maker_instance_for_market, stop_and_cancel_market_maker_instance
    from app.schemas.api import ConfirmExecuteRequest
    market = await session.scalar(select(Market).where(Market.symbol == symbol.upper()))
    if market is None or body.action not in ('start', 'stop'):
        raise HTTPException(422, '无效的币对或动作')
    if body.action == 'start':
        result = await start_maker_instance_for_market(market, request, user, session)
        await session.commit()
        selected, _, _ = await current_strategy_config(session, request, market)
        if selected.strategy_key in INTERNAL_MAKER_STRATEGIES:
            return request.app.state.contract_ladder_service.read(market.symbol)
        return result
    selected, _, _ = await current_strategy_config(session, request, market)
    if selected.strategy_key in INTERNAL_MAKER_STRATEGIES:
        svc = request.app.state.contract_ladder_service
        record = svc.records.get(market.symbol)
        if not record:
            raise HTTPException(409, 'LADDER 实例未完成配置')
        draft = deepcopy(record['config']); draft['enabled'] = body.action == 'start'
        try:
            return await svc.publish(market.symbol, draft, record['desired_version'], user.id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return await stop_and_cancel_market_maker_instance(symbol, ConfirmExecuteRequest(confirm_execute=True), request, user, session)


@router.get('/admin/liquidity/templates/SIMPLE_BBO', dependencies=[Depends(get_admin_user)])
async def read_bbo_template(session=Depends(get_db_session)):
    from app.models.strategy_template import StrategyTemplate
    from app.services.strategy_config_service import ensure_strategy_templates
    from app.services.simple_bbo import BBOParameters
    await ensure_strategy_templates(session)
    row = await session.scalar(select(StrategyTemplate).where(StrategyTemplate.strategy_key == 'SIMPLE_BBO'))
    result = BBOParameters.model_validate(row.default_config_json).model_dump(mode='json')
    await session.commit()
    return result




@router.put('/admin/liquidity/templates/SIMPLE_BBO', dependencies=[Depends(get_admin_user)])
async def save_bbo_template(body: dict, session=Depends(get_db_session)):
    from app.services.simple_bbo import BBOParameters
    body = BBOParameters.model_validate(body)
    from app.models.strategy_template import StrategyTemplate
    from app.services.strategy_config_service import ensure_strategy_templates
    await ensure_strategy_templates(session)
    row = await session.scalar(select(StrategyTemplate).where(StrategyTemplate.strategy_key == 'SIMPLE_BBO'))
    row.default_config_json = body.model_dump(mode='json')
    await session.commit()
    return row.default_config_json


@router.get('/admin/liquidity/plugins', dependencies=[Depends(get_admin_user)])
async def installed_maker_plugins():
    from app.services.maker_plugins import installed_plugins
    return {'api_version': 1, 'items': [
        {k: v for k, v in p.manifest.items() if k != 'entrypoints'}
        for p in installed_plugins().values()
    ]}


@router.get('/admin/liquidity/makers/{symbol}/parameters', dependencies=[Depends(get_admin_user)])
async def read_plugin_parameters(symbol: str, request: Request):
    from app.services.maker_plugins import get_plugin
    svc = request.app.state.contract_ladder_service
    record = svc.records.get(symbol.upper())
    if record is None:
        raise HTTPException(404, '策略实例尚未配置')
    plugin = get_plugin(record['strategy_key'])
    fields = plugin.manifest.get('parameters', [])
    return {'strategy_key': plugin.key, 'expected_version': record['desired_version'],
            'fields': fields, 'config': deepcopy(record['config'])}


class PluginParametersSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy_key: str
    expected_version: int = Field(ge=0)
    parameters: dict


@router.put('/admin/liquidity/makers/{symbol}/parameters')
@maker_serialized
async def save_plugin_parameters(symbol: str, body: PluginParametersSave, request: Request, user=Depends(get_admin_user)):
    from app.services.contract_ladder_service import VersionConflict
    record = await read_plugin_parameters(symbol, request)
    if body.strategy_key != record['strategy_key']:
        raise HTTPException(409, '策略已切换，请重新读取参数')
    allowed = {f['path'] for f in record['fields']}
    if not set(body.parameters) <= allowed:
        raise HTTPException(422, '参数不在策略声明的可编辑范围内')
    draft = record['config']
    for path, value in body.parameters.items():
        cursor = draft
        parts = path.split('.')
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value
    try:
        return await request.app.state.contract_ladder_service.publish(symbol.upper(), draft, body.expected_version, user.id)
    except VersionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc

@router.get('/admin/liquidity/overview', dependencies=[Depends(get_admin_user)])
async def overview(request: Request, session=Depends(get_db_session)):
    """Configuration list: three bounded queries, no account funding probes or order serialization."""
    from app.api.admin import maker_instance_status
    markets=(await session.scalars(select(Market).order_by(Market.symbol))).all()
    configs=(await session.scalars(select(MarketStrategyConfig).where(MarketStrategyConfig.is_enabled.is_(True)))).all()
    selected={c.market_id:c.strategy_key for c in configs}
    bindings=(await session.execute(select(MarketBotAccount,User.username).join(User,User.id==MarketBotAccount.user_id).where(MarketBotAccount.is_enabled.is_(True),User.is_active.is_(True)))).all()
    groups={}
    for binding,name in bindings:
        if binding.strategy_role=='paper_default':continue
        groups.setdefault(binding.market_id,[]).append((binding,name))
    manager=getattr(request.app.state,'contract_ladder_service',None)
    items=[];instances=[]
    for market in markets:
        key=selected.get(market.id,'NONE')
        worker=manager.workers.get(market.symbol) if manager else None
        state={'state':worker.state,'enabled':bool(worker.config.get('enabled'))} if worker else None
        if worker:
            status={'symbol':market.symbol,'running':state['enabled'],'status':worker.state}
        else:
            legacy=maker_instance_status(market.symbol,request,configured_strategy_version=key)
            status={'symbol':market.symbol,'running':bool(legacy.get('running')),'status':legacy.get('status','STOPPED')}
        instances.append(status)
        accounts=groups.get(market.id,[])
        makers=[{'uid':b.user_id,'username':name} for b,name in accounts if b.role=='maker' and ((b.strategy_role=='CONTRACT_LADDER')==(key in INTERNAL_MAKER_STRATEGIES))]
        choices=strategy_choices(market.product_type)
        items.append({'symbol':market.symbol,'product_type':market.product_type,'strategy_key':key,'choices':choices,
            'parameter_editors':{k:'schema' if get_plugin(k).manifest.get('parameters') else 'legacy' for k in choices},
            'source_exchange':'binance','source_symbol':market.price_source_symbol or market.symbol.removesuffix('-PERP'),
            'readiness':{'scope':'configuration','ok':False,'blockers':[]},'ladder':state,
            'account_slots':{k:ACCOUNT_SLOTS[k] for k in choices},'maker_accounts':makers,
            'flow_accounts':[{'uid':b.user_id,'username':name} for b,name in accounts if b.role=='flow']})
    return {'items':items,'instances':instances,'switch_supported':True,'switches':switch_states(request.app.state.runtime)}
