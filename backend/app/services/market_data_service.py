from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.constants import SIDE_BUY, SIDE_SELL, ZERO
from app.core.decimal_utils import decimal_to_str, quantize_scale
from app.core.time_utils import ensure_utc, to_millis
from app.models.display_kline import DisplayKline
from app.models.kline import Kline
from app.models.trade import Trade
from app.services.persistence_contract import platform_durable_contract

INTERVAL_SECONDS = {
    "1s": 1,
    "5s": 5,
    "15s": 15,
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}

BOOTSTRAP_SEED_SOURCE = "bootstrap_seed"


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
    source_counts: dict[str, int] = field(default_factory=dict)
    source_volumes: dict[str, Decimal] = field(default_factory=dict)
    source_quote_volumes: dict[str, Decimal] = field(default_factory=dict)
    is_closed: bool = False
    carried_forward: bool = False
    sampled: bool = False
    display_only: bool = False
    persisted: bool = False
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None

    @property
    def source(self) -> str:
        sources = {source for source, count in self.source_counts.items() if count > 0}
        if not sources:
            return "unknown"
        if len(sources) == 1:
            return next(iter(sources))
        return "mixed"


class MarketDataService:
    def __init__(self) -> None:
        self.display_history_since_ms: dict[str, int] = {}
        self.recent_trades: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=200))
        # Display-only synthetic FLOW tape.  It is deliberately isolated from
        # canonical recent trades and live_klines so it cannot become a mark /
        # risk input or leak into SQLite through persist_kline.
        self.display_only_recent_trades: dict[str, deque[dict]] = defaultdict(
            lambda: deque(maxlen=200)
        )
        # Public chart aggregates consume real fills AND virtual FLOW, like
        # public TAS. This series/table is never a position, risk or Trade fact;
        # real-only live_klines remains isolated for those existing consumers.
        self.display_only_klines: dict[str, dict[str, deque[KlinePoint]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=500))
        )
        self.live_klines: dict[str, dict[str, deque[KlinePoint]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=500))
        )
        # Time-driven sampled history is a separate in-memory namespace.  It
        # is the only source used by sampled public charts; live_klines remains
        # available for strict-durable compatibility and risk-aware callers.
        sampled_max = max(
            100,
            int(getattr(settings, "history_1m_keep_per_market", 10_080)),
        )
        self.sampled_klines: dict[str, dict[str, deque[KlinePoint]]] = defaultdict(
            lambda: defaultdict(lambda: deque(maxlen=sampled_max))
        )
        self.sampled_last_price: dict[str, Decimal] = {}
        self.sampled_last_source: dict[str, str] = {}
        self.sampled_last_sample_at_ms: dict[str, int] = {}
        self.sampled_kline_persist_state: dict[tuple[str, str, datetime], int] = {}
        self.sampling_drop_count = 0
        self.latest_stats: dict[str, dict] = {}
        self.seq_by_market: dict[str, int] = defaultdict(int)
        self.orderbook_updated_at_ms: dict[str, int] = {}
        self.orderbook_signature_by_market: dict[str, tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]] = {}
        self.orderbook_last_emitted_by_market: dict[
            str,
            tuple[int, tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]],
        ] = {}
        self.orderbook_same_seq_diff_count: dict[str, int] = defaultdict(int)
        self.orderbook_seq_rollback_count: dict[str, int] = defaultdict(int)
        self.kline_persist_state: dict[tuple[int, str, str], tuple[datetime, int]] = {}
        self.minute_dirty: dict[tuple[str, str], set[datetime]] = defaultdict(set)
        self.closed_minute_ack: dict[tuple[int, str], datetime] = {}
        self.display_kline_persist_state: dict[tuple[int, str, str], tuple[datetime, int]] = {}

    def next_seq(self, symbol: str) -> int:
        self.seq_by_market[symbol] += 1
        return self.seq_by_market[symbol]

    def record_orderbook_update(self, symbol: str, ts_ms: int | None = None) -> tuple[int, int]:
        normalized = symbol.upper()
        seq = self.next_seq(normalized)
        if ts_ms is None:
            ts_ms = to_millis(datetime.now(tz=UTC))
        self.orderbook_updated_at_ms[normalized] = ts_ms
        return seq, ts_ms

    def orderbook_meta(self, symbol: str) -> tuple[int, int]:
        normalized = symbol.upper()
        return self.seq_by_market[normalized], self.orderbook_updated_at_ms.get(normalized, 0)

    @staticmethod
    def orderbook_signature(
        snapshot: dict[str, list[list[str]]],
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        return (
            tuple((str(price), str(quantity)) for price, quantity in snapshot.get("bids", [])),
            tuple((str(price), str(quantity)) for price, quantity in snapshot.get("asks", [])),
        )

    def sync_orderbook_snapshot_meta(
        self,
        symbol: str,
        snapshot: dict[str, list[list[str]]],
        ts_ms: int | None = None,
    ) -> tuple[int, int]:
        normalized = symbol.upper()
        signature = self.orderbook_signature(snapshot)
        if self.orderbook_signature_by_market.get(normalized) != signature:
            seq, updated_at_ms = self.record_orderbook_update(normalized, ts_ms=ts_ms)
            self.orderbook_signature_by_market[normalized] = signature
        else:
            seq, updated_at_ms = self.orderbook_meta(normalized)
        self._record_orderbook_emission(normalized, seq, signature)
        return seq, updated_at_ms

    def _record_orderbook_emission(
        self,
        symbol: str,
        seq: int,
        signature: tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]],
    ) -> None:
        last = self.orderbook_last_emitted_by_market.get(symbol)
        if last is not None:
            last_seq, last_signature = last
            if seq < last_seq:
                self.orderbook_seq_rollback_count[symbol] += 1
            elif seq == last_seq and signature != last_signature:
                self.orderbook_same_seq_diff_count[symbol] += 1
        self.orderbook_last_emitted_by_market[symbol] = (seq, signature)

    def orderbook_invariant_snapshot(self, symbol: str) -> dict:
        normalized = symbol.upper()
        seq, updated_at_ms = self.orderbook_meta(normalized)
        return {
            "seq": seq,
            "updated_at_ms": updated_at_ms,
            "same_seq_diff_count": int(self.orderbook_same_seq_diff_count.get(normalized, 0)),
            "seq_rollback_count": int(self.orderbook_seq_rollback_count.get(normalized, 0)),
        }

    def _bucket(self, ts: datetime, interval: str) -> tuple[datetime, datetime]:
        seconds = INTERVAL_SECONDS[interval]
        epoch = int(ensure_utc(ts).timestamp())
        start = epoch - (epoch % seconds)
        open_time = datetime.fromtimestamp(start, tz=UTC)
        close_time = open_time + timedelta(seconds=seconds) - timedelta(milliseconds=1)
        return open_time, close_time

    @staticmethod
    def _sample_payload(item: KlinePoint, *, symbol: str, interval: str, market_id: int | None = None) -> dict:
        return {
            "market_id": market_id,
            "symbol": symbol.upper(),
            "interval": interval,
            "open_time_ms": to_millis(item.open_time),
            "close_time_ms": to_millis(item.close_time),
            "open": decimal_to_str(item.open),
            "high": decimal_to_str(item.high),
            "low": decimal_to_str(item.low),
            "close": decimal_to_str(item.close),
            "volume": decimal_to_str(item.volume),
            "quote_volume": decimal_to_str(item.quote_volume),
            "trade_count": int(item.trade_count),
            "source": item.source,
            "carried_forward": bool(item.carried_forward),
            "display_only": bool(item.display_only),
            "sampled": bool(item.sampled),
            "is_closed": bool(item.is_closed),
            "updated_at_ms": to_millis(datetime.now(tz=UTC)),
            "payload": {
                "source_counts": dict(item.source_counts),
                "source_volumes": {key: decimal_to_str(value) for key, value in item.source_volumes.items()},
                "source_quote_volumes": {
                    key: decimal_to_str(value) for key, value in item.source_quote_volumes.items()
                },
            },
        }

    def _sampled_append_or_update_minute(
        self,
        symbol: str,
        *,
        price: Decimal,
        quantity: Decimal,
        quote_volume: Decimal,
        ts: datetime,
        source: str,
    ) -> list[KlinePoint]:
        """Advance the 1m time series, inserting carried-forward gaps."""

        open_time, close_time = self._bucket(ts, "1m")
        series = self.sampled_klines[symbol]["1m"]
        changed: list[KlinePoint] = []
        carried = source == "carried_forward"
        current = series[-1] if series else None
        if current is not None and current.open_time < open_time:
            current.is_closed = True
            changed.append(current)
            cursor = current.open_time + timedelta(minutes=1)
            previous_close = current.close
            while cursor < open_time:
                gap = KlinePoint(
                    open_time=cursor,
                    close_time=cursor + timedelta(minutes=1) - timedelta(milliseconds=1),
                    open=previous_close,
                    high=previous_close,
                    low=previous_close,
                    close=previous_close,
                    volume=ZERO,
                    quote_volume=ZERO,
                    trade_count=0,
                    source_counts={"carried_forward": 1},
                    source_volumes={"carried_forward": ZERO},
                    source_quote_volumes={"carried_forward": ZERO},
                    is_closed=True,
                    carried_forward=True,
                    sampled=True,
                    display_only=True,
                )
                series.append(gap)
                changed.append(gap)
                previous_close = gap.close
                cursor += timedelta(minutes=1)
            current = None
        if current is None or current.open_time != open_time:
            current = KlinePoint(
                open_time=open_time,
                close_time=close_time,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=quantity,
                quote_volume=quote_volume,
                trade_count=0 if quantity == ZERO and not carried else 1,
                source_counts={source: 1},
                source_volumes={source: quantity},
                source_quote_volumes={source: quote_volume},
                is_closed=False,
                carried_forward=carried,
                sampled=True,
                display_only=True,
            )
            series.append(current)
            changed.append(current)
        else:
            current.high = max(current.high, price)
            current.low = min(current.low, price)
            current.close = price
            current.volume += quantity
            current.quote_volume += quote_volume
            if quantity != ZERO:
                current.trade_count += 1
            current.source_counts[source] = current.source_counts.get(source, 0) + 1
            current.source_volumes[source] = current.source_volumes.get(source, ZERO) + quantity
            current.source_quote_volumes[source] = current.source_quote_volumes.get(source, ZERO) + quote_volume
            current.carried_forward = current.carried_forward or carried
            changed.append(current)
        return changed

    def _sampled_rebuild_aggregate(self, symbol: str, interval: str, sample: KlinePoint) -> KlinePoint | None:
        seconds = INTERVAL_SECONDS[interval]
        open_time, close_time = self._bucket(sample.open_time, interval)
        one_minute = self.sampled_klines[symbol]["1m"]
        members = [
            item for item in one_minute
            if open_time <= item.open_time < close_time and item.is_closed
        ]
        if not members:
            return None
        series = self.sampled_klines[symbol][interval]
        current = series[-1] if series else None
        if current is None or current.open_time != open_time:
            if current is not None:
                current.is_closed = True
            current = KlinePoint(
                open_time=open_time,
                close_time=close_time,
                open=members[0].open,
                high=max(item.high for item in members),
                low=min(item.low for item in members),
                close=members[-1].close,
                volume=sum((item.volume for item in members), ZERO),
                quote_volume=sum((item.quote_volume for item in members), ZERO),
                trade_count=sum(item.trade_count for item in members),
                source_counts={"sampled_1m": len(members)},
                source_volumes={"sampled_1m": sum((item.volume for item in members), ZERO)},
                source_quote_volumes={"sampled_1m": sum((item.quote_volume for item in members), ZERO)},
                is_closed=False,
                carried_forward=all(item.carried_forward for item in members),
                sampled=True,
                display_only=True,
            )
            series.append(current)
        else:
            current.open = members[0].open
            current.high = max(item.high for item in members)
            current.low = min(item.low for item in members)
            current.close = members[-1].close
            current.volume = sum((item.volume for item in members), ZERO)
            current.quote_volume = sum((item.quote_volume for item in members), ZERO)
            current.trade_count = sum(item.trade_count for item in members)
            current.carried_forward = all(item.carried_forward for item in members)
        # A 5m/1h candle becomes final only after its last 1m member closes.
        current.is_closed = (sample.open_time >= open_time + timedelta(seconds=seconds - 60)) and sample.is_closed
        return current

    def sample_price(
        self,
        symbol: str,
        price: Decimal | None,
        ts: datetime,
        *,
        source: str = "published_mid",
        market_id: int | None = None,
        quantity: Decimal = ZERO,
        quote_volume: Decimal | None = None,
    ) -> list[dict]:
        """Record one 1-second price observation without touching SQLAlchemy."""

        normalized = str(symbol).upper()
        ts = ensure_utc(ts)
        if price is None:
            price = self.sampled_last_price.get(normalized)
            source = "carried_forward"
        if price is None:
            self.sampling_drop_count += 1
            return []
        price = Decimal(price)
        if price <= ZERO:
            self.sampling_drop_count += 1
            return []
        quote_volume = price * quantity if quote_volume is None else Decimal(quote_volume)
        self.sampled_last_price[normalized] = price
        self.sampled_last_source[normalized] = source
        self.sampled_last_sample_at_ms[normalized] = to_millis(ts)
        changed = self._sampled_append_or_update_minute(
            normalized,
            price=price,
            quantity=Decimal(quantity),
            quote_volume=quote_volume,
            ts=ts,
            source=source,
        )
        current_minute = self.sampled_klines[normalized]["1m"][-1]
        aggregates: list[tuple[str, KlinePoint]] = []
        for closed_point in changed:
            if not closed_point.is_closed:
                continue
            for interval in ("5m", "1h"):
                if interval == "1h" and not settings.history_1h_enabled:
                    continue
                aggregate = self._sampled_rebuild_aggregate(normalized, interval, closed_point)
                if aggregate is not None and not any(item is aggregate for _, item in aggregates):
                    aggregates.append((interval, aggregate))
        points: list[tuple[str, KlinePoint]] = [("1m", item) for item in changed]
        points.extend(aggregates)
        result: list[dict] = []
        now_ms = to_millis(datetime.now(tz=UTC))
        for interval, item in points:
            key = (normalized, interval, item.open_time)
            last_write = self.sampled_kline_persist_state.get(key, 0)
            if item.is_closed or now_ms - last_write >= int(settings.history_unclosed_upsert_seconds) * 1000:
                self.sampled_kline_persist_state[key] = now_ms
                result.append(self._sample_payload(item, symbol=normalized, interval=interval, market_id=market_id))
        return result

    def sampled_kline_items(self, symbol: str, interval: str, limit: int) -> list[dict]:
        return self._kline_items(
            self.sampled_klines,
            str(symbol).upper(),
            interval,
            limit,
            include_seed=True,
        )

    def ingest_trade(
        self,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        side: str,
        ts: datetime,
        trade_id: str | None = None,
        price_scale: int | None = None,
        qty_scale: int | None = None,
        source: str = "unknown",
    ) -> None:
        ts = ensure_utc(ts)
        if price_scale is not None:
            price = quantize_scale(price, price_scale)
        if qty_scale is not None:
            quantity = quantize_scale(quantity, qty_scale)
        trade = {
            "trade_id": trade_id,
            "price": decimal_to_str(price),
            "quantity": decimal_to_str(quantity),
            "side": side,
            "ts": to_millis(ts),
            "source": source,
        }
        self.recent_trades[symbol].appendleft(trade)
        observer = getattr(self, "liquidity_map_observer", None)
        if observer is not None:
            try:
                observer(symbol, trade)
            except Exception:
                # Observation is best effort; it must never reject a trade.
                pass
        self._ingest_kline_trade(
            self.live_klines,
            symbol=symbol,
            price=price,
            quantity=quantity,
            ts=ts,
            source=source,
        )
        # Public charts consume the same real + virtual event stream as TAS.
        # live_klines remains real-only for risk and accounting consumers.
        self._ingest_kline_trade(self.display_only_klines, symbol=symbol, price=price,
            quantity=quantity, ts=ts, source=source)
        if str(settings.persistence_mode).strip().lower() == "sampled":
            self.sample_price(
                symbol,
                price,
                ts,
                source=source,
                quantity=quantity,
                quote_volume=price * quantity,
            )

    def _ingest_kline_trade(
        self,
        container: dict[str, dict[str, deque[KlinePoint]]],
        *,
        symbol: str,
        price: Decimal,
        quantity: Decimal,
        ts: datetime,
        source: str,
    ) -> None:
        quote_amount = price * quantity
        for interval in INTERVAL_SECONDS:
            open_time, close_time = self._bucket(ts, interval)
            series = container[symbol][interval]
            current = series[-1] if series else None
            late = current is not None and open_time < current.open_time
            if late:
                current = next((p for p in reversed(series) if p.open_time == open_time), None)
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
                    source_counts={source: 1},
                    source_volumes={source: quantity},
                    source_quote_volumes={source: quote_amount},
                    is_closed=late, first_trade_at=ts, last_trade_at=ts,
                )
                if late:
                    items = sorted([*series, current], key=lambda p: p.open_time)
                    series.clear(); series.extend(items)
                else:
                    series.append(current)
            else:
                current.high = max(current.high, price)
                current.low = min(current.low, price)
                if current.first_trade_at is None or ts < current.first_trade_at:
                    current.open = price if current.first_trade_at is not None else current.open
                    current.first_trade_at = ts
                if current.last_trade_at is None or ts >= current.last_trade_at:
                    current.close = price
                    current.last_trade_at = ts
                current.volume += quantity
                current.quote_volume += quote_amount
                current.trade_count += 1
                current.source_counts[source] = current.source_counts.get(source, 0) + 1
                current.source_volumes[source] = current.source_volumes.get(source, ZERO) + quantity
                current.source_quote_volumes[source] = current.source_quote_volumes.get(source, ZERO) + quote_amount
            current.persisted = False
            if interval == "1m":
                kind = Kline.__tablename__ if container is self.live_klines else DisplayKline.__tablename__
                dirty = self.minute_dirty[(symbol, kind)]
                dirty.add(open_time)
                if len(dirty) > 500:
                    dirty.intersection_update(p.open_time for p in series)

    def recent_trade_items(self, symbol: str, limit: int, *, include_seed: bool = False) -> list[dict]:
        items = list(self.recent_trades[symbol])
        if not include_seed:
            items = [item for item in items if item.get("source") != BOOTSTRAP_SEED_SOURCE]
        return items[:limit]

    def ingest_display_trade(
        self,
        symbol: str,
        *,
        price: Decimal,
        quantity: Decimal,
        side: str,
        ts: datetime,
        trade_id: str,
        price_scale: int | None = None,
        qty_scale: int | None = None,
        source: str = "synthetic_flow",
    ) -> dict:
        """Append a public-only tape item without touching canonical K-lines."""
        ts = ensure_utc(ts)
        if price_scale is not None:
            price = quantize_scale(price, price_scale)
        if qty_scale is not None:
            quantity = quantize_scale(quantity, qty_scale)
        item = {
            "trade_id": trade_id,
            "price": decimal_to_str(price),
            "quantity": decimal_to_str(quantity),
            "side": side,
            "ts": to_millis(ts),
            "source": source,
            "synthetic": True,
            "execution_mode": "synthetic_ephemeral",
            "durable": False,
            "financial_effect": False,
            "persistence": "ephemeral_tape",
        }
        self.display_only_recent_trades[symbol].appendleft(item)
        self._ingest_kline_trade(
            self.display_only_klines,
            symbol=symbol,
            price=price,
            quantity=quantity,
            ts=ts,
            source=source,
        )
        return dict(item)

    def display_recent_trade_items(
        self,
        symbol: str,
        limit: int,
        *,
        include_seed: bool = False,
    ) -> list[dict]:
        """Merge canonical and display-only tape for public presentation."""
        canonical = self.recent_trade_items(symbol, limit, include_seed=include_seed)
        display_only = list(self.display_only_recent_trades[symbol])
        cutoff = self.display_history_since_ms.get(symbol, 0)
        merged = [item for item in canonical + display_only if int(item.get("ts") or 0) > cutoff]
        merged.sort(key=lambda item: int(item.get("ts") or 0), reverse=True)
        seen_ids: set[str] = set()
        result: list[dict] = []
        for item in merged:
            trade_id = str(item.get("trade_id") or "")
            if trade_id and trade_id in seen_ids:
                continue
            if trade_id:
                seen_ids.add(trade_id)
            result.append(dict(item))
            if len(result) >= limit:
                break
        return result

    def _kline_items(
        self,
        container: dict[str, dict[str, deque[KlinePoint]]],
        symbol: str,
        interval: str,
        limit: int,
        *,
        include_seed: bool,
        display_only: bool = False,
    ) -> list[dict]:
        items = list(container[symbol][interval])
        if not include_seed:
            items = [
                item for item in items
                if item.source_counts.get(BOOTSTRAP_SEED_SOURCE, 0) <= 0
            ]
        items = items[-limit:]
        serialized: list[dict] = []
        for item in items:
            payload = {
                "open_time": to_millis(item.open_time),
                "close_time": to_millis(item.close_time),
                "open": decimal_to_str(item.open),
                "high": decimal_to_str(item.high),
                "low": decimal_to_str(item.low),
                "close": decimal_to_str(item.close),
                "volume": decimal_to_str(item.volume),
                "quote_volume": decimal_to_str(item.quote_volume),
                "trade_count": item.trade_count,
                "source": item.source,
                "source_counts": dict(item.source_counts),
                "source_volumes": {
                    source: decimal_to_str(volume)
                    for source, volume in item.source_volumes.items()
                    if volume > ZERO
                },
                "source_quote_volumes": {
                    source: decimal_to_str(volume)
                    for source, volume in item.source_quote_volumes.items()
                    if volume > ZERO
                },
                "is_closed": item.is_closed or item.close_time < datetime.now(tz=UTC),
                "carried_forward": bool(item.carried_forward),
                "sampled": bool(item.sampled),
                "display_only": bool(item.display_only or display_only),
            }
            if display_only or item.sampled:
                payload.update(
                    {
                        "synthetic": True,
                        "execution_mode": "sampled_display" if item.sampled else "synthetic_ephemeral",
                        "durable": item.persisted,
                        "financial_effect": False,
                        "persistence": "minute_aggregate" if item.persisted else "sampled_history" if item.sampled else "display_kline",
                    }
                )
            serialized.append(payload)
        return serialized

    def get_klines(self, symbol: str, interval: str, limit: int, *, include_seed: bool = False) -> list[dict]:
        """Canonical trade K-lines used by settlement/risk-aware callers."""
        return self._kline_items(
            self.live_klines,
            symbol,
            interval,
            limit,
            include_seed=include_seed,
        )

    def get_public_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        include_seed: bool = False,
    ) -> list[dict]:
        """Merge canonical candles with isolated synthetic display history."""
        if str(settings.persistence_mode).strip().lower() == "sampled":
            sampled = self.sampled_kline_items(symbol, interval, limit)
            if sampled:
                return sampled
        canonical = self.get_klines(symbol, interval, limit, include_seed=include_seed)
        display = self._kline_items(
            self.display_only_klines,
            symbol,
            interval,
            limit,
            include_seed=include_seed,
            display_only=True,
        )
        # The public series includes BOTH real fills and virtual prints.
        # Restored complete public candles also supersede real-only history;
        # adding the two would double-count the real component.
        by_open_time = {int(item["open_time"]): item for item in canonical}
        by_open_time.update({int(item["open_time"]): item for item in display})
        return [by_open_time[key] for key in sorted(by_open_time)[-limit:]]

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
            .where(Trade.market_id == market_id, Trade.executed_at >= since, Trade.source != BOOTSTRAP_SEED_SOURCE)
            .order_by(Trade.executed_at.asc())
        )
        trades = list(result.scalars())
        if not trades:
            return {
                "open_24h": decimal_to_str(last_price),
                "high_24h": decimal_to_str(last_price),
                "low_24h": decimal_to_str(last_price),
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
            "high_24h": decimal_to_str(max(Decimal(trade.price) for trade in trades)),
            "low_24h": decimal_to_str(min(Decimal(trade.price) for trade in trades)),
            "change_24h": decimal_to_str(change_24h),
            "change_24h_pct": decimal_to_str(change_24h_pct),
            "volume_24h": decimal_to_str(volume_24h),
            "quote_volume_24h": decimal_to_str(quote_volume_24h),
        }

    async def persist_kline(self, session: AsyncSession, market_id: int, symbol: str, interval: str) -> None:
        if platform_durable_contract():
            return  # The minute writer persists closed snapshots outside trading.
        if interval not in {"1m", "5m"}:
            return
        series = self.live_klines[symbol][interval]
        if not series:
            return
        item = series[-1]
        now_ms = to_millis(datetime.now(tz=UTC))
        min_interval_ms = max(0, int(settings.kline_persist_min_interval_ms))
        persist_key = (market_id, symbol, interval)
        last_open_time, last_persisted_at_ms = self.kline_persist_state.get(persist_key, (datetime.min.replace(tzinfo=UTC), 0))
        if (
            min_interval_ms > 0
            and last_open_time == item.open_time
            and now_ms - last_persisted_at_ms < min_interval_ms
        ):
            return
        existing = await session.scalar(
            select(Kline).where(
                Kline.market_id == market_id,
                Kline.interval == interval,
                Kline.open_time == item.open_time,
            )
        )
        source = BOOTSTRAP_SEED_SOURCE if item.source_counts.get(BOOTSTRAP_SEED_SOURCE, 0) > 0 else item.source
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
                    source=source,
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
            existing.source = source
        self.kline_persist_state[persist_key] = (item.open_time, now_ms)

    async def broadcast_klines(self, ws, symbol: str) -> None:
        for interval in ("1s", "5s", "15s", "1m", "5m", "15m"):
            items = self.get_public_klines(symbol, interval, 1)
            if items:
                await ws.broadcast_public("kline", symbol, {"channel": "kline", "type": "update",
                    "symbol": symbol, "interval": interval, "kline": items[-1]}, interval=interval)

    async def persist_closed_minutes(self, session, market_id, symbol, *, now=None):
        """At most two aggregate rows/minute; caller acknowledges AFTER commit.

        OHLCV/count remain cheap and useful for strategy tests. No per-trade
        SQL, no synthetic Trade/ledger, no writes for the unfinished minute.
        """
        now = ensure_utc(now or datetime.now(tz=UTC))
        tickets = []
        for model, container in ((Kline, self.live_klines), (DisplayKline, self.display_only_klines)):
            key = (market_id, model.__tablename__)
            ack = self.closed_minute_ack.get(key, datetime.min.replace(tzinfo=UTC))
            # Copy scalars before awaiting; current aggregation can continue.
            points = [dict(open_time=p.open_time, close_time=p.close_time, open=p.open,
                high=p.high, low=p.low, close=p.close, volume=p.volume, quote_volume=p.quote_volume,
                trade_count=p.trade_count, source="public_trades" if model is DisplayKline else p.source)
                for p in container[symbol]["1m"] if (p.open_time > ack or p.open_time in self.minute_dirty[(symbol, model.__tablename__)]) and p.close_time < now][:60]
            if not points:
                continue
            existing = {ensure_utc(row.open_time): row for row in (await session.scalars(select(model).where(
                model.market_id == market_id, model.interval == "1m",
                model.open_time >= points[0]["open_time"], model.open_time <= points[-1]["open_time"]))).all()}
            for values in points:
                row = existing.get(values["open_time"])
                if row is None:
                    session.add(model(market_id=market_id, interval="1m", **values))
                else:
                    for name, value in values.items(): setattr(row, name, value)
            tickets.append((key, symbol, [(p["open_time"], p["trade_count"]) for p in points]))
        return tickets

    def acknowledge_closed_minutes(self, tickets):
        for key, symbol, revisions in tickets:
            container = self.live_klines if key[1] == Kline.__tablename__ else self.display_only_klines
            current = {p.open_time: p for p in container[symbol]["1m"]}
            for time, count in revisions:
                if time in current and current[time].trade_count == count:
                    current[time].persisted = True
                    current[time].is_closed = True
                    self.minute_dirty[(symbol, key[1])].discard(time)
            self.closed_minute_ack[key] = max(self.closed_minute_ack.get(key, revisions[-1][0]), revisions[-1][0])

    def _restore_minutes(self, container, symbol, points):
        """Rebuild larger chart periods from persisted minute aggregates."""
        minute = {p.open_time: p for p in container[symbol]["1m"]}
        minute.update({p.open_time: p for p in points})
        container[symbol]["1m"].clear()
        container[symbol]["1m"].extend(minute[k] for k in sorted(minute))
        for interval, seconds in INTERVAL_SECONDS.items():
            if seconds <= 60: continue
            buckets = {}
            for p in container[symbol]["1m"]:
                start, end = self._bucket(p.open_time, interval)
                c = buckets.get(start)
                if c is None:
                    c = KlinePoint(start, end, p.open, p.high, p.low, p.close, p.volume,
                        p.quote_volume, p.trade_count, dict(p.source_counts), dict(p.source_volumes),
                        dict(p.source_quote_volumes), is_closed=end < datetime.now(tz=UTC), persisted=p.persisted)
                    buckets[start] = c
                else:
                    c.high=max(c.high,p.high);c.low=min(c.low,p.low);c.close=p.close
                    c.volume+=p.volume;c.quote_volume+=p.quote_volume;c.trade_count+=p.trade_count
                    c.persisted = c.persisted and p.persisted
                    for attr in ("source_counts","source_volumes","source_quote_volumes"):
                        dest=getattr(c,attr)
                        for name,value in getattr(p,attr).items():dest[name]=dest.get(name,0)+value
            container[symbol][interval].clear()
            container[symbol][interval].extend(buckets[k] for k in sorted(buckets))

    async def persist_display_kline(
        self,
        session: AsyncSession,
        market_id: int,
        symbol: str,
        interval: str,
    ) -> None:
        """Persist only the low-frequency public chart aggregate.

        The row intentionally lives outside ``klines`` so no canonical market,
        mark-price or settlement caller can consume synthetic FLOW as a real
        trade candle.
        """
        if interval not in {"1m", "5m"}:
            return
        series = self.display_only_klines[symbol][interval]
        if not series:
            return
        item = series[-1]
        if item.source_counts.get("virtual_volume", 0):
            return  # Virtual FLOW candles are memory-only, even if display persistence is enabled.
        now_ms = to_millis(datetime.now(tz=UTC))
        min_interval_ms = max(0, int(settings.kline_persist_min_interval_ms))
        persist_key = (market_id, symbol, interval)
        last_open_time, last_persisted_at_ms = self.display_kline_persist_state.get(
            persist_key,
            (datetime.min.replace(tzinfo=UTC), 0),
        )
        if (
            min_interval_ms > 0
            and last_open_time == item.open_time
            and now_ms - last_persisted_at_ms < min_interval_ms
        ):
            return
        existing = await session.scalar(
            select(DisplayKline).where(
                DisplayKline.market_id == market_id,
                DisplayKline.interval == interval,
                DisplayKline.open_time == item.open_time,
            )
        )
        if existing is None:
            session.add(
                DisplayKline(
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
                    source="synthetic_flow",
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
            existing.source = "synthetic_flow"
        self.display_kline_persist_state[persist_key] = (item.open_time, now_ms)

    async def load_persisted_klines(self, session: AsyncSession, market_id: int, symbol: str, *, include_seed: bool = False) -> None:
        conditions = [Kline.market_id == market_id, Kline.interval.in_(["1m", "5m"])]
        if not include_seed:
            conditions.append(Kline.source != BOOTSTRAP_SEED_SOURCE)
        result = await session.execute(
            select(Kline)
            .where(*conditions)
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
                source_counts={item.source: item.trade_count},
                source_volumes={item.source: Decimal(item.volume)},
                source_quote_volumes={item.source: Decimal(item.quote_volume)},
                is_closed=True,
            )
            series = self.live_klines[symbol][item.interval]
            if not series or series[-1].open_time != point.open_time:
                series.append(point)

    async def load_persisted_display_klines(
        self,
        session: AsyncSession,
        market_id: int,
        symbol: str,
    ) -> None:
        public_rows = list((await session.scalars(select(DisplayKline).where(
            DisplayKline.market_id == market_id, DisplayKline.interval == "1m",
            DisplayKline.source == "public_trades").order_by(DisplayKline.open_time.desc()).limit(500))).all())
        if public_rows:
            points = [KlinePoint(ensure_utc(r.open_time), ensure_utc(r.close_time), Decimal(r.open),
                Decimal(r.high), Decimal(r.low), Decimal(r.close), Decimal(r.volume), Decimal(r.quote_volume),
                r.trade_count, {"public_trades":r.trade_count}, {"public_trades":Decimal(r.volume)},
                {"public_trades":Decimal(r.quote_volume)}, is_closed=True, persisted=True) for r in reversed(public_rows)]
            self._restore_minutes(self.display_only_klines, symbol, points)
            self.minute_dirty[(symbol, DisplayKline.__tablename__)].difference_update(p.open_time for p in points)
            self.closed_minute_ack[(market_id, DisplayKline.__tablename__)] = points[-1].open_time
            return
        result = await session.execute(
            select(DisplayKline)
            .where(
                DisplayKline.market_id == market_id,
                DisplayKline.interval.in_(["1m", "5m"]),
            )
            .order_by(DisplayKline.open_time.asc())
            .limit(1000)
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
                source_counts={"synthetic_flow": item.trade_count},
                source_volumes={"synthetic_flow": Decimal(item.volume)},
                source_quote_volumes={"synthetic_flow": Decimal(item.quote_volume)},
                is_closed=True,
            )
            series = self.display_only_klines[symbol][item.interval]
            if not any(p.open_time == point.open_time for p in series):
                series.append(point)

    async def load_from_trades(
        self,
        session: AsyncSession,
        market_id: int,
        symbol: str,
        limit: int = 5000,
        price_precision: int | None = None,
        qty_precision: int | None = None,
        include_seed: bool = False,
    ) -> bool:
        conditions = [Trade.market_id == market_id]
        cutoff = self.display_history_since_ms.get(symbol, 0)
        if cutoff:
            conditions.append(Trade.executed_at > datetime.fromtimestamp(cutoff / 1000, tz=UTC))
        if not include_seed:
            conditions.append(Trade.source != BOOTSTRAP_SEED_SOURCE)
        result = await session.execute(
            select(Trade)
            .where(*conditions)
            .order_by(Trade.executed_at.desc(), Trade.id.desc())
            .limit(limit)
        )
        items = list(reversed(list(result.scalars())))
        if not items:
            return False
        self.recent_trades[symbol].clear()
        self.live_klines[symbol].clear()
        self.display_only_klines[symbol].clear()
        for trade in items:
            self.ingest_trade(
                symbol,
                price=Decimal(trade.price),
                quantity=Decimal(trade.quantity),
                side=trade.taker_side,
                ts=ensure_utc(trade.executed_at),
                trade_id=trade.trade_id,
                price_scale=price_precision,
                qty_scale=qty_precision,
                source=trade.source,
            )
        return True
