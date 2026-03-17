from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import SIDE_BUY, SIDE_SELL, ZERO
from app.core.decimal_utils import decimal_to_str
from app.core.time_utils import ensure_utc, to_millis
from app.models.kline import Kline
from app.models.trade import Trade

INTERVAL_SECONDS = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1d": 86400,
}


@dataclass(slots=True)
class KlinePoint:
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int
    is_closed: bool = False


class MarketDataService:
    def __init__(self) -> None:
        self.recent_trades: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=200))
        self.live_klines: dict[str, dict[str, deque[KlinePoint]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=500))
        )
        self.latest_stats: dict[str, dict] = {}
        self.seq_by_market: dict[str, int] = defaultdict(int)

    def next_seq(self, symbol: str) -> int:
        self.seq_by_market[symbol] += 1
        return self.seq_by_market[symbol]

    def _bucket(self, ts: datetime, interval: str) -> tuple[datetime, datetime]:
        seconds = INTERVAL_SECONDS[interval]
        epoch = int(ensure_utc(ts).timestamp())
        start = epoch - (epoch % seconds)
        open_time = datetime.fromtimestamp(start, tz=UTC)
        close_time = open_time + timedelta(seconds=seconds) - timedelta(milliseconds=1)
        return open_time, close_time

    def ingest_trade(self, symbol: str, price: Decimal, quantity: Decimal, side: str, ts: datetime, trade_id: str | None = None) -> None:
        ts = ensure_utc(ts)
        trade = {
            "trade_id": trade_id,
            "price": decimal_to_str(price),
            "quantity": decimal_to_str(quantity),
            "side": side,
            "ts": to_millis(ts),
        }
        self.recent_trades[symbol].appendleft(trade)
        quote_amount = price * quantity
        for interval in INTERVAL_SECONDS:
            open_time, close_time = self._bucket(ts, interval)
            series = self.live_klines[symbol][interval]
            current = series[-1] if series else None
            if current is None or current.open_time != open_time:
                if current is not None:
                    current.is_closed = True
                current = KlinePoint(
                    open_time=open_time,
                    close_time=close_time,
                    open=price,
                    high=price,
                    low=price,
                    close=price,
                    volume=quantity,
                    quote_volume=quote_amount,
                    trade_count=1,
                    is_closed=False,
                )
                series.append(current)
            else:
                current.high = max(current.high, price)
                current.low = min(current.low, price)
                current.close = price
                current.volume += quantity
                current.quote_volume += quote_amount
                current.trade_count += 1

    def recent_trade_items(self, symbol: str, limit: int) -> list[dict]:
        return list(self.recent_trades[symbol])[:limit]

    def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict]:
        items = list(self.live_klines[symbol][interval])[-limit:]
        return [
            {
                "open_time": to_millis(item.open_time),
                "close_time": to_millis(item.close_time),
                "open": decimal_to_str(item.open),
                "high": decimal_to_str(item.high),
                "low": decimal_to_str(item.low),
                "close": decimal_to_str(item.close),
                "volume": decimal_to_str(item.volume),
                "quote_volume": decimal_to_str(item.quote_volume),
                "trade_count": item.trade_count,
                "is_closed": item.is_closed,
            }
            for item in items
        ]

    def compute_stats(self, symbol: str, orderbook: dict[str, list[list[str]]]) -> dict:
        bids = orderbook["bids"]
        asks = orderbook["asks"]
        best_bid = Decimal(bids[0][0]) if bids else None
        best_ask = Decimal(asks[0][0]) if asks else None
        mid = ((best_bid + best_ask) / Decimal("2")) if best_bid is not None and best_ask is not None else best_bid or best_ask or ZERO
        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else ZERO
        spread_pct = (spread / mid * Decimal("100")) if mid else ZERO

        def depth_within(limit_pct: Decimal, levels: list[list[str]], side: str) -> Decimal:
            if mid <= ZERO:
                return ZERO
            total = ZERO
            for price_str, qty_str in levels:
                price = Decimal(price_str)
                qty = Decimal(qty_str)
                if side == SIDE_BUY and price < mid * (Decimal("1") - limit_pct):
                    continue
                if side == SIDE_SELL and price > mid * (Decimal("1") + limit_pct):
                    continue
                total += price * qty
            return total

        bid_depth = sum(Decimal(level[0]) * Decimal(level[1]) for level in bids[:10]) if bids else ZERO
        ask_depth = sum(Decimal(level[0]) * Decimal(level[1]) for level in asks[:10]) if asks else ZERO
        imbalance = bid_depth / (bid_depth + ask_depth) if (bid_depth + ask_depth) > ZERO else ZERO
        stats = {
            "best_bid": decimal_to_str(best_bid or ZERO),
            "best_ask": decimal_to_str(best_ask or ZERO),
            "mid_price": decimal_to_str(mid),
            "spread": decimal_to_str(spread),
            "spread_pct": decimal_to_str(spread_pct),
            "depth_amount_0_5pct": decimal_to_str(depth_within(Decimal("0.005"), bids, SIDE_BUY) + depth_within(Decimal("0.005"), asks, SIDE_SELL)),
            "depth_amount_2pct": decimal_to_str(depth_within(Decimal("0.02"), bids, SIDE_BUY) + depth_within(Decimal("0.02"), asks, SIDE_SELL)),
            "book_imbalance": decimal_to_str(imbalance),
            "ts": to_millis(datetime.now(tz=UTC)),
        }
        self.latest_stats[symbol] = stats
        return stats

    async def compute_24h_stats(self, session: AsyncSession, market_id: int, last_price: Decimal) -> dict:
        since = datetime.now(tz=UTC) - timedelta(hours=24)
        result = await session.execute(
            select(Trade)
            .where(Trade.market_id == market_id, Trade.executed_at >= since)
            .order_by(Trade.executed_at.asc())
        )
        trades = list(result.scalars())
        if not trades:
            return {
                "open_24h": decimal_to_str(last_price),
                "change_24h": decimal_to_str(ZERO),
                "change_24h_pct": decimal_to_str(ZERO),
                "volume_24h": decimal_to_str(ZERO),
                "quote_volume_24h": decimal_to_str(ZERO),
            }

        open_24h = Decimal(trades[0].price)
        close_24h = Decimal(trades[-1].price)
        volume_24h = sum(Decimal(trade.quantity) for trade in trades)
        quote_volume_24h = sum(Decimal(trade.quote_amount) for trade in trades)
        change_24h = close_24h - open_24h
        change_24h_pct = (change_24h / open_24h * Decimal("100")) if open_24h > ZERO else ZERO
        return {
            "open_24h": decimal_to_str(open_24h),
            "change_24h": decimal_to_str(change_24h),
            "change_24h_pct": decimal_to_str(change_24h_pct),
            "volume_24h": decimal_to_str(volume_24h),
            "quote_volume_24h": decimal_to_str(quote_volume_24h),
        }

    async def persist_kline(self, session: AsyncSession, market_id: int, symbol: str, interval: str) -> None:
        if interval not in {"1m", "5m"}:
            return
        series = self.live_klines[symbol][interval]
        if not series:
            return
        item = series[-1]
        existing = await session.scalar(
            select(Kline).where(
                Kline.market_id == market_id,
                Kline.interval == interval,
                Kline.open_time == item.open_time,
            )
        )
        if existing is None:
            session.add(
                Kline(
                    market_id=market_id,
                    interval=interval,
                    open_time=item.open_time,
                    close_time=item.close_time,
                    open=item.open,
                    high=item.high,
                    low=item.low,
                    close=item.close,
                    volume=item.volume,
                    quote_volume=item.quote_volume,
                    trade_count=item.trade_count,
                )
            )
        else:
            existing.close_time = item.close_time
            existing.high = item.high
            existing.low = item.low
            existing.close = item.close
            existing.volume = item.volume
            existing.quote_volume = item.quote_volume
            existing.trade_count = item.trade_count

    async def load_persisted_klines(self, session: AsyncSession, market_id: int, symbol: str) -> None:
        result = await session.execute(
            select(Kline)
            .where(Kline.market_id == market_id, Kline.interval.in_(["1m", "5m"]))
            .order_by(Kline.open_time.asc())
            .limit(500)
        )
        for item in result.scalars():
            point = KlinePoint(
                open_time=ensure_utc(item.open_time),
                close_time=ensure_utc(item.close_time),
                open=Decimal(item.open),
                high=Decimal(item.high),
                low=Decimal(item.low),
                close=Decimal(item.close),
                volume=Decimal(item.volume),
                quote_volume=Decimal(item.quote_volume),
                trade_count=item.trade_count,
                is_closed=True,
            )
            series = self.live_klines[symbol][item.interval]
            if not series or series[-1].open_time != point.open_time:
                series.append(point)

    async def load_from_trades(self, session: AsyncSession, market_id: int, symbol: str, limit: int = 5000) -> bool:
        result = await session.execute(
            select(Trade)
            .where(Trade.market_id == market_id)
            .order_by(Trade.executed_at.asc())
            .limit(limit)
        )
        items = list(result.scalars())
        if not items:
            return False
        self.recent_trades[symbol].clear()
        self.live_klines[symbol].clear()
        for trade in items:
            self.ingest_trade(
                symbol,
                price=Decimal(trade.price),
                quantity=Decimal(trade.quantity),
                side=trade.taker_side,
                ts=ensure_utc(trade.executed_at),
                trade_id=trade.trade_id,
            )
        return True
