from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse
from .service import TARGET, TRADE_KIND
from .store import RETENTION_MS

router = APIRouter(prefix='/liquidity-map')
WEB = Path(__file__).parent / 'web'


def service_for(request):
    service = getattr(request.app.state.runtime, 'liquidity_map', None)
    if service is None or service.store is None:
        raise HTTPException(503, '流动性地图正在启动')
    return service


async def archive_query(service, function, *args):
    try:
        await asyncio.wait_for(service.query_slots.acquire(), timeout=0.2)
    except asyncio.TimeoutError:
        raise HTTPException(429, '历史读取繁忙，请稍后重试')
    try:
        return await asyncio.to_thread(function, *args)
    finally:
        service.query_slots.release()


@router.get('')
async def redirect():
    return RedirectResponse('/liquidity-map/')


@router.get('/')
async def page():
    return FileResponse(WEB/'index.html', headers={'Cache-Control': 'no-store'})


@router.get('/static/{name}')
async def asset(name: str):
    if name not in {'app.js', 'app.css'}:
        raise HTTPException(404)
    return FileResponse(WEB/name, headers={'Cache-Control': 'no-cache'})


@router.get('/api/symbols')
async def symbols(request: Request):
    service = service_for(request)
    await service.refresh_markets()
    return {'targets': [{'id': TARGET, 'label': '沙盒交易所', 'symbols': sorted(service.metadata)}]}


@router.get('/api/status')
async def status(request: Request):
    return service_for(request).status()


def bounds(request, service):
    query = request.query_params
    symbol = query.get('symbol', '')
    if symbol not in service.metadata or query.get('target', TARGET) != TARGET:
        raise HTTPException(400, '请选择当前沙盒运行中的币对')
    now = int(time.time()*1000)
    try:
        asof = int(query.get('asOfMs', now))
        start = int(query.get('from', asof-RETENTION_MS))
        end = int(query.get('to', asof))
    except (ValueError, TypeError):
        raise HTTPException(400, '时间必须为毫秒整数')
    if not 0 <= start < end <= asof <= now+1000 or end-start > RETENTION_MS:
        raise HTTPException(400, '历史范围最多 10 分钟，不能查询未来')
    return symbol, start, end, now, dict(mode='replay', target=TARGET, symbol=symbol,
        asOfMs=asof, frozenToMs=asof, requestedFromMs=start, requestedToMs=end,
        retentionSeconds=600, resolutionMs=1000)


@router.get('/api/replay/history')
async def history(request: Request, maxColumns: int = Query(1000, ge=1, le=1000)):
    service = service_for(request)
    symbol, start, end, now, payload = bounds(request, service)
    # Archived samples mark the end of [t-1000,t); UI replay uses bucket starts.
    records = await archive_query(service, service.store.read, symbol, start+1000, min(end+1000, now//1000*1000+1), now)
    tiles, size, last = [], 0, None
    for item in records:
        column = dict(item['column'], t=item['t']-1000, sourceCount=1, coverageMs=1000,
                      quality='gap' if item['column'].get('gap') else 'complete')
        tile = dict(series=dict(item.get('view', {}), target=TARGET, symbol=symbol),
                    resolutionMs=1000, columns=[column])
        cost = len(json.dumps(tile, separators=(',', ':')).encode())
        if tiles and (size+cost > 1_200_000 or len(tiles) >= min(maxColumns, 100)):
            break
        tiles.append(tile)
        size += cost
        last = int(item['t'])
    # Missing samples are explicit gaps. Never fill outages with stale liquidity.
    represented = {tile['columns'][0]['t'] for tile in tiles}
    query_end = last if last is not None and last < end and len(tiles) < len(records) else end
    gaps = [[t, min(t+1000, query_end)] for t in range((start+999)//1000*1000, query_end, 1000) if t not in represented]
    payload.update(tiles=tiles, gaps=gaps, errors=[], nextFromMs=query_end if query_end < end else None,
                   partialRanges=[], storage=service.store.status())
    return payload


@router.get('/api/replay/trades')
async def trades(request: Request, minNotional: float = Query(0, ge=0),
                 maxNotionalExclusive: float | None = Query(None, gt=0),
                 maxItems: int = Query(5000, ge=1, le=5000), cursor: str | None = None):
    service = service_for(request)
    symbol, start, end, now, payload = bounds(request, service)
    records = await archive_query(service, service.store.read, symbol, max(0,start-1000), min(now+1,end+1001), now)
    tape = {str(t['id']):t for row in records for t in row.get('trades', []) if
            start <= t['tradeTime'] < end and t['notionalValue'] >= minNotional and
            (maxNotionalExclusive is None or t['notionalValue'] < maxNotionalExclusive)}
    ordered = sorted(tape.values(), key=lambda t:(t['tradeTime'],t['id']))
    if cursor:
        try:
            ts, ident = [int(part) for part in cursor.split(':')]
        except (ValueError, TypeError):
            raise HTTPException(400, '无效成交游标')
        ordered = [t for t in ordered if (t['tradeTime'],t['id']) > (ts,ident)]
    page = ordered[:min(maxItems, 2000)]
    next_cursor = f"{page[-1]['tradeTime']}:{page[-1]['id']}" if len(ordered)>len(page) else None
    payload.update(kind=TRADE_KIND, sourceEventType='sandbox_trade', applicationAggregation=False,
                   minNotional=minNotional, archiveMinNotional=0, maxNotionalExclusive=maxNotionalExclusive,
                   trades=page, nextCursor=next_cursor)
    return payload


@router.websocket('/ws')
async def stream(socket: WebSocket):
    service = getattr(socket.app.state.runtime, 'liquidity_map', None)
    await socket.accept()
    if service is None or service.store is None or service.clients >= 16:
        await socket.close(code=1013)
        return
    service.clients += 1
    symbol = socket.query_params.get('symbol') or socket.query_params.get('marketId') or next(iter(service.metadata), '')
    epoch, last_t = 0, -1
    async def send(payload):
        await asyncio.wait_for(socket.send_json(payload), timeout=5)
    async def subscribe(next_symbol):
        nonlocal symbol, epoch, last_t
        if next_symbol not in service.views:
            await send(dict(type='error', message='请选择当前沙盒币对', code='SUBSCRIBE_REJECTED'))
            return
        symbol, epoch, last_t = next_symbol, epoch+1, -1
        await send(dict(type='reset', target=TARGET,symbol=symbol,streamEpoch=service.epoch,
                        subscriptionEpoch=epoch,reason='沙盒订单簿 · 最近 10 分钟'))
        snapshot = await archive_query(service, service.snapshot, symbol)
        await send(dict(snapshot, subscriptionEpoch=epoch))
        last_t = snapshot['t']
    try:
        await subscribe(symbol)
        while True:
            try:
                command = await asyncio.wait_for(socket.receive_json(), timeout=0.5)
                if isinstance(command, dict) and command.get('type') == 'subscribe':
                    await subscribe(str(command.get('symbol', '')))
            except asyncio.TimeoutError:
                pass
            view = service.views.get(symbol)
            frame = view.last if view else None
            if frame and frame['t'] != last_t:
                public = {k:v for k,v in frame.items() if k != 'rawBook'}
                await send(dict(public, subscriptionEpoch=epoch))
                last_t = frame['t']
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, HTTPException):
        pass
    finally:
        service.clients -= 1
