"""Public exchange-style data. Public volume includes labeled virtual prints; financial facts remain separate."""
from datetime import UTC,datetime,timedelta
from decimal import Decimal
from urllib.parse import quote
from fastapi import APIRouter,Depends,HTTPException,Query,Request,Response
from sqlalchemy import select,func
from app.api.deps import get_db_session
from app.core.config import settings
from app.models.market import Market
from app.models.trade import Trade

router=APIRouter(prefix='/market-data',tags=['public-market-data'])
EXCLUDED_SOURCES=('bootstrap_seed','synthetic','synthetic_flow','synthetic_no_fill','virtual_volume','seed_book')

def matched_filter():
    return Trade.source.notin_(EXCLUDED_SOURCES)

def number(value,precision=8):
    if value is None:return '0'
    return format(Decimal(str(value)).quantize(Decimal(1).scaleb(-precision)),'f')

async def market_or_404(session,symbol):
    market=await session.scalar(select(Market).where(Market.symbol==symbol.upper(),Market.is_active.is_(True)))
    if market is None:raise HTTPException(404,'market not found')
    return market

@router.get('/summary')
async def summary(request:Request,response:Response,session=Depends(get_db_session)):
    response.headers['Cache-Control']='public, max-age=1'
    markets=(await session.scalars(select(Market).where(Market.is_active.is_(True)).order_by(Market.id))).all()
    since=datetime.now(UTC)-timedelta(hours=24)
    rows=(await session.execute(select(Trade.market_id,func.sum(Trade.quantity),func.sum(Trade.quote_amount),func.min(Trade.price),func.max(Trade.price)).where(Trade.executed_at>=since,matched_filter()).group_by(Trade.market_id))).all()
    volumes={r[0]:r[1:] for r in rows}
    latest_ids=select(func.max(Trade.id)).where(matched_filter()).group_by(Trade.market_id)
    latest={t.market_id:t for t in (await session.scalars(select(Trade).where(Trade.id.in_(latest_ids)))).all()}
    tape = getattr(request.app.state.runtime, 'public_trade_tape', None)
    virtual_volumes, virtual_latest = await tape.snapshot() if tape else ({}, {})
    origin=(settings.public_origin or str(request.base_url)).rstrip('/')+settings.public_base_path.rstrip('/')
    result=[]
    for market in markets:
        book,_,_=await request.app.state.runtime.orderbook_snapshot(market.symbol,1)
        vol=volumes.get(market.id,(0,0,None,None));last=latest.get(market.id)
        virtual = virtual_volumes.get(market.symbol, (0,0,None,None))
        virtual_last = virtual_latest.get(market.symbol)
        last_ts = int(last.executed_at.replace(tzinfo=UTC).timestamp()*1000) if last else None
        use_virtual = virtual_last is not None and (last_ts is None or virtual_last['timestamp'] >= last_ts)
        last_price = virtual_last['price'] if use_virtual else last.price if last else None
        low = min((v for v in (vol[2],virtual[2]) if v is not None),default=None)
        high = max((v for v in (vol[3],virtual[3]) if v is not None),default=None)
        result.append({'trading_pairs' :market.symbol,'ticker_id':market.symbol,'base_currency':market.base_asset,'quote_currency':market.quote_asset,
            'type':'spot' if market.product_type=='SPOT' else 'perpetual','product_type':market.product_type,
            'last_price':number(last_price,market.price_precision),
            'base_volume':number(Decimal(str(vol[0] or 0))+Decimal(str(virtual[0] or 0)),market.qty_precision),'quote_volume':number(Decimal(str(vol[1] or 0))+Decimal(str(virtual[1] or 0)),8),
            'matched_base_volume':number(vol[0],market.qty_precision),'matched_quote_volume':number(vol[1],8),
            'virtual_base_volume':number(virtual[0],market.qty_precision),'virtual_quote_volume':number(virtual[1],8),
            'highest_bid':book['bids'][0][0] if book['bids'] else None,'lowest_ask':book['asks'][0][0] if book['asks'] else None,
            'lowest_price_24h':number(low,market.price_precision),'highest_price_24h':number(high,market.price_precision),
            'price_tick':number(market.price_tick,market.price_precision),'quantity_step':number(market.qty_step,market.qty_precision),
            'price_precision':market.price_precision,'quantity_precision':market.qty_precision,
            'isFrozen':int(market.paper_status!='TRADING' or market.contract_trading_mode=='paused'),
            'market_url':origin+'/paper/trade/'+quote(market.symbol,safe=''),
            'is_simulated':True,'volume_source':'matched_orders_and_virtual_volume','virtual_volume_included':True,
            'last_trade_source':'virtual_volume' if use_virtual else 'matched_order' if last else None,
            'last_trade_timestamp':virtual_last['timestamp'] if use_virtual else last_ts})
    return result

@router.get('/ticker')
async def ticker(request:Request,response:Response,session=Depends(get_db_session)):
    return {item['ticker_id']:item for item in await summary(request,response,session)}

@router.get('/assets')
async def assets(session=Depends(get_db_session)):
    markets=(await session.scalars(select(Market).where(Market.is_active.is_(True)))).all()
    return {asset:{'name':asset,'symbol':asset,'can_withdraw':False,'can_deposit':False,'is_simulated':True} for asset in sorted({a for m in markets for a in (m.base_asset,m.quote_asset)})}

@router.get('/orderbook/{symbol}')
async def orderbook(symbol:str,request:Request,depth:int=Query(default=20,ge=1,le=200),session=Depends(get_db_session)):
    market=await market_or_404(session,symbol)
    book,sequence,ts=await request.app.state.runtime.orderbook_snapshot(market.symbol,depth)
    return {'ticker_id':market.symbol,'type':'spot' if market.product_type=='SPOT' else 'perpetual','timestamp':ts,'sequence':sequence,'bids':book['bids'],'asks':book['asks'],'is_simulated':True}

@router.get('/trades/{symbol}')
async def trades(symbol:str,request:Request,limit:int=Query(default=100,ge=1,le=500),session=Depends(get_db_session)):
    market=await market_or_404(session,symbol)
    rows=(await session.scalars(select(Trade).where(Trade.market_id==market.id,matched_filter()).order_by(Trade.executed_at.desc(),Trade.id.desc()).limit(limit))).all()
    matched = [{'trade_id':t.trade_id,'price':number(t.price,market.price_precision),'base_volume':number(t.quantity,market.qty_precision),'quote_volume':number(t.quote_amount,8),'timestamp':int(t.executed_at.replace(tzinfo=UTC).timestamp()*1000),'type':t.taker_side,'source':'matched_order','is_virtual':False,'financial_effect':True,'is_simulated':True} for t in rows]
    tape = getattr(request.app.state.runtime, 'public_trade_tape', None)
    virtual = await tape.recent(market.symbol,limit) if tape else []
    return sorted(matched+virtual,key=lambda item:(item['timestamp'],item['trade_id']),reverse=True)[:limit]
