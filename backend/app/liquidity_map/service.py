"""Read committed in-memory books; observe canonical trades through a bounded tap.

Projection, compression and filesystem work run in a worker, never in the
matching path. A single sampler serves every viewer and every active market.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import logging
from pathlib import Path
import time

from sqlalchemy import select, or_
from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from .core import LocalOrderBook, SymbolMeta, ViewAggregator
from .heatmap import HeatmapRing
from .store import ObservationStore, RETENTION_MS

log = logging.getLogger('liquidity_map')
TARGET = 'sandbox'
TRADE_KIND = 'sandbox-trades-v1'


@dataclass
class MarketView:
    meta: SymbolMeta
    book: LocalOrderBook
    aggregator: ViewAggregator = field(default_factory=ViewAggregator)
    ring: HeatmapRing = field(default_factory=lambda: HeatmapRing(window_seconds=1, column_interval_ms=1000))
    last: dict | None = None
    seen_trades: dict = field(default_factory=dict)


class LiquidityMapService:
    def __init__(self, runtime, session_factory, root: Path):
        self.runtime = runtime
        self.session_factory = session_factory
        self.root = root
        self.store: ObservationStore | None = None
        self.views: dict[str, MarketView] = {}
        self.metadata: dict[str, SymbolMeta] = {}
        self.trade_queue = deque(maxlen=10_000)
        self.trade_drops = 0
        self.last_error = None
        self.clients = 0
        self.query_slots = asyncio.Semaphore(2)
        self.epoch = int(time.time() * 1000)
        self.sample_ms = 0.0

    def observe_trade(self, symbol: str, trade: dict):
        # Called inline: copy only public data into a bounded queue, no I/O.
        if trade.get('source') in {'bootstrap_seed', 'seed', 'display_only', 'synthetic_flow'}:
            return
        if len(self.trade_queue) == self.trade_queue.maxlen:
            self.trade_drops += 1
        self.trade_queue.append((symbol, dict(trade)))

    async def refresh_markets(self):
        async with self.session_factory() as session:
            markets = (await session.execute(select(Market).where(or_(Market.is_active.is_(True), select(MarketBotAccount.id).where(MarketBotAccount.market_id == Market.id, MarketBotAccount.role == "maker", MarketBotAccount.is_enabled.is_(True)).exists())))).scalars().all()
            self.metadata = {m.symbol: SymbolMeta(m.symbol, m.base_asset, m.quote_asset,
                str(m.product_type), Decimal(m.price_tick), int(m.price_precision), int(m.qty_precision),
                target=TARGET, venue_label='沙盒交易所') for m in markets}
        for symbol in list(self.views):
            if symbol not in self.metadata:
                self.views.pop(symbol, None)

    async def run(self):
        try:
            self.store = await asyncio.to_thread(ObservationStore, self.root)
            self.runtime.market_data.liquidity_map_observer = self.observe_trade
            refresh_at = 0.0
            while True:
                start = time.monotonic()
                try:
                    if start >= refresh_at:
                        await self.refresh_markets()
                        refresh_at = start + 10
                    # Immutable published books: no matcher locks or DB snapshots.
                    books = {symbol: self.runtime.published_orderbooks.get(symbol) for symbol in self.metadata}
                    trades = list(self.trade_queue)
                    self.trade_queue.clear()
                    await asyncio.to_thread(self.sample, books, trades, int(time.time()*1000))
                    self.last_error = None
                except Exception as exc:
                    self.last_error = f'{type(exc).__name__}: {exc}'[:200]
                    log.warning('observation sampler: %s', self.last_error)
                self.sample_ms = (time.monotonic()-start)*1000
                await asyncio.sleep(max(0.05, 1-(time.monotonic()-start)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f'{type(exc).__name__}: {exc}'[:200]
            log.exception('liquidity map unavailable; trading remains independent')
        finally:
            if getattr(self.runtime.market_data, 'liquidity_map_observer', None) == self.observe_trade:
                self.runtime.market_data.liquidity_map_observer = None

    def sample(self, books: dict, trades: list, now: int):
        assert self.store is not None
        self.store.prune(now)
        grouped: dict[str, list] = {}
        for symbol, trade in trades:
            grouped.setdefault(symbol, []).append(trade)
        for symbol, meta in list(self.metadata.items()):
            published = books.get(symbol)
            view = self.views.get(symbol)
            if view is None or view.meta != meta:
                view = MarketView(meta, LocalOrderBook(symbol))
                self.views[symbol] = view
            stamp = now // 1000 * 1000
            public_trades = self.convert_trades(view, grouped.get(symbol, []), now)
            try:
                if published is None or not published.bids or not published.asks:
                    frame = self.empty_frame(symbol, stamp)
                else:
                    bids = [list(row) for row in published.bids[:5000]]
                    asks = [list(row) for row in published.asks[:5000]]
                    view.book.replace_snapshot(bids, asks, stamp, limit=5000)
                    view.book.snapshot_bid_complete = len(published.bids) <= 5000
                    view.book.snapshot_ask_complete = len(published.asks) <= 5000
                    projection = view.aggregator.project(view.book, meta)
                    if projection is None:
                        frame = self.empty_frame(symbol, stamp)
                    else:
                        column = view.ring.capture_projection(projection, stamp)
                        config = self.view_config(view)
                        frame = dict(type='frame', t=stamp, target=TARGET, symbol=symbol,
                            streamEpoch=self.epoch, meta=self.meta_payload(meta), view=config,
                            status=dict(overall='LIVE', depth='LIVE', trade='LIVE', resyncCount=0, depthReconnects=0, tradeReconnects=0),
                            dom=view.aggregator.aggregate_projection(view.book, meta, projection),
                            mid=float(projection.mid), column=column, trades=public_trades,
                            bubbles=public_trades, droppedTrades=self.trade_drops,
                            rawBook=dict(bids=bids, asks=asks, seq=published.seq,
                                updatedAt=published.updated_at_ms, sampledAt=stamp))
                        if public_trades:
                            frame['lastTrade'] = public_trades[-1]
                grid = view.aggregator.grid
                if grid is not None:
                    for trade in public_trades:
                        trade['bucket'] = grid.bucket_index(Decimal(trade['price']))
                frame.update(trades=public_trades, bubbles=public_trades, droppedTrades=self.trade_drops)
                if public_trades:
                    frame['lastTrade'] = public_trades[-1]
                if published is not None and 'rawBook' not in frame:
                    frame['rawBook'] = dict(bids=[list(row) for row in published.bids[:5000]],
                        asks=[list(row) for row in published.asks[:5000]], seq=published.seq,
                        updatedAt=published.updated_at_ms, sampledAt=stamp)
                view.last = frame
                # DOM is redundant with rawBook and the projected column; store once.
                archived = {k:v for k,v in frame.items() if k not in {'dom', 'bubbles'}}
                self.store.write(archived, now)
            except Exception as exc:
                view.last = self.empty_frame(symbol, stamp, str(exc)[:160])
                self.store.errors += 1
                self.store.last_error = str(exc)[:160]

    def convert_trades(self, view: MarketView, trades: list, now: int):
        public_trades = []
        for trade in trades:
            ts = int(trade.get('ts') or 0)
            if not now-RETENTION_MS <= ts <= now+1000:
                continue
            key = str(trade.get('trade_id') or json.dumps(trade, sort_keys=True))
            if key in view.seen_trades:
                continue
            view.seen_trades[key] = ts
            price, qty = Decimal(trade['price']), Decimal(trade['quantity'])
            event_id = int.from_bytes(hashlib.sha256(key.encode()).digest()[:6], 'big')
            public_trades.append(dict(id=event_id, aggregateId=event_id, sequence=event_id,
                eventTime=ts, tradeTime=ts, price=str(price), quantity=str(qty),
                notionalValue=float(price*qty), side=trade['side'], sourceEventType='sandbox_trade',
                sourceFillCount=1, applicationAggregation=False, firstTradeId=event_id,
                lastTradeId=event_id))
        view.seen_trades = {k:t for k,t in view.seen_trades.items() if t >= now-RETENTION_MS}
        if len(view.seen_trades) > 100_000:
            view.seen_trades = dict(list(view.seen_trades.items())[-100_000:])
        return public_trades

    def meta_payload(self, meta: SymbolMeta):
        return dict(target=TARGET, symbol=meta.symbol, marketId=meta.symbol, venueLabel='沙盒交易所',
                    baseAsset=meta.base_asset, quoteAsset=meta.quote_asset, tickSize=str(meta.tick_size),
                    fixedTickSize=str(meta.tick_size), gridQuantum=str(meta.tick_size), priceRule='fixed_tick',
                    priceDecimals=meta.price_decimals, maxPriceDecimals=meta.price_decimals,
                    quantityDecimals=meta.quantity_decimals)

    def view_config(self, view: MarketView):
        grid = view.aggregator.grid
        return dict(spacingPct=str(view.aggregator.spacing_pct), rangePct=str(view.aggregator.range_pct),
            rangeMode=view.aggregator.range_mode, gridEpoch=view.aggregator.grid_epoch,
            gridAnchor=float(grid.anchor) if grid else None, gridRatio=float(grid.ratio) if grid else None,
            effectiveSpacingPct=float(grid.effective_spacing_pct) if grid else None,
            tickSize=str(view.meta.tick_size), heatmapWindowSeconds=600, heatmapColumnMs=1000,
            heatmapMaxColumns=600, heatmapLevelsPerSide=2000, domIntervalMs=1000, bubbleBucketMs=1000,
            historyAvailable=self.store is not None, rawTradeHistoryKind=TRADE_KIND,
            rawTradeSourceEventType='sandbox_trade', archiveMinNotional=0)

    def empty_frame(self, symbol: str, stamp: int, reason='等待沙盒盘口'):
        view = self.views[symbol]
        return dict(type='frame', t=stamp, target=TARGET, symbol=symbol, streamEpoch=self.epoch,
            meta=self.meta_payload(view.meta), view=self.view_config(view), status=dict(overall='WAITING'),
            dom=dict(ready=False,bids=[],asks=[],gaps=[]), column=dict(t=stamp,gap=True,reason=reason),
            trades=[], bubbles=[])

    def snapshot(self, symbol: str):
        view = self.views[symbol]
        frame = dict(view.last or self.empty_frame(symbol, int(time.time()*1000)))
        now = int(time.time()*1000)
        history = self.store.read(symbol, now-RETENTION_MS, now+1, now) if self.store else []
        # Grid changes across restart are available through replay's explicit remap.
        grid = frame.get('view', {})
        columns = [h['column'] for h in history if h.get('column') and
                   h.get('view', {}).get('gridAnchor') == grid.get('gridAnchor') and
                   h.get('view', {}).get('gridRatio') == grid.get('gridRatio')]
        tape = [trade for h in history for trade in h.get('trades', [])][-10_000:]
        frame.update(type='snapshot', columns=columns[-600:], trades=tape[-400:], bubbles=tape)
        frame.pop('rawBook', None)
        frame.pop('column', None)
        return frame

    def status(self):
        return dict(source='sandbox_committed_orderbook', markets=list(self.metadata),
            sampleIntervalMs=1000, lastSampleMs=round(self.sample_ms,2), clients=self.clients,
            tradeDrops=self.trade_drops, lastError=self.last_error,
            storage=self.store.status() if self.store else None)
