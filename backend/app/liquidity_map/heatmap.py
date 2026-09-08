"""短窗热图 ring 与逐条成交气泡的有界显示队列。

硬约束：只在内存里保留连接后的短窗真实盘口，关进程即丢；不写磁盘、不回补历史。
断线 / resync 会写入一条 gap 列，前端据此显示缺口，而不是把缺口画成零流动性。
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from typing import Any

from .core import (
    GridProjection,
    LocalOrderBook,
    StablePercentGrid,
    project_order_book,
)

# 单列采样不再按 mid ± N 截断；与 DOM 同源采整本订单簿。
# 此上限仅用于极端保护（远超 1000 档 snapshot 时仍裁剪）。
HEATMAP_LEVELS_MAX = 2000
MIN_DISPLAY_AGGTRADE_NOTIONAL = Decimal("0")


class HeatmapRing:
    """时间 × 价格桶的挂单厚度短窗环形缓存。

    价格轴直接使用 `StablePercentGrid` 的绝对 bucket index，因此热图列、
    右侧聚合 DOM 和价轴共用同一套坐标，天然对齐；网格 epoch 变化时整段作废。
    """

    def __init__(
        self,
        *,
        window_seconds: int,
        column_interval_ms: int,
        levels_per_side: int = 240,
    ) -> None:
        if window_seconds <= 0 or column_interval_ms <= 0:
            raise ValueError("热图窗口与列间隔必须大于 0")
        # levels_per_side 保留兼容，但 capture 默认采整簿；此值只作软上限。
        self.levels_per_side = max(1, min(HEATMAP_LEVELS_MAX, levels_per_side))
        self.window_seconds = window_seconds
        self.column_interval_ms = column_interval_ms
        self.max_columns = max(
            2, int(window_seconds * 1000 / column_interval_ms)
        )
        self.columns: deque[dict[str, Any]] = deque(maxlen=self.max_columns)
        self.sequence = 0
        self.grid_epoch = 0

    def reset(self, grid_epoch: int) -> None:
        self.columns.clear()
        self.grid_epoch = grid_epoch

    def mark_gap(self, timestamp_ms: int, reason: str) -> None:
        """记录一段缺口；重复的缺口不再累积列。"""

        if self.columns and self.columns[-1].get("gap"):
            self.columns[-1]["t"] = timestamp_ms
            return
        self.sequence += 1
        self.columns.append(
            {
                "seq": self.sequence,
                "t": timestamp_ms,
                "gap": True,
                "reason": reason,
            }
        )

    def capture(
        self,
        book: LocalOrderBook,
        grid: StablePercentGrid,
        grid_epoch: int,
        timestamp_ms: int,
    ) -> dict[str, Any] | None:
        """按当前订单簿采样一列；与 DOM 同源，采整本簿而非 mid 附近切片。"""

        projection = project_order_book(book, grid, grid_epoch)
        if projection is None:
            return None
        return self.capture_projection(projection, timestamp_ms)

    def capture_projection(
        self,
        projection: GridProjection,
        timestamp_ms: int,
    ) -> dict[str, Any]:
        """从共享未裁剪投影生成一列，仍执行热图自己的极端距离保护。"""

        grid_epoch = projection.grid_epoch
        if grid_epoch != self.grid_epoch:
            self.reset(grid_epoch)
        center = projection.center
        best_bid_index = projection.best_bid
        best_ask_index = projection.best_ask
        # Sandbox ladders can be thousands of ticks apart. Bound actual sparse
        # levels, not distance in ticks, so far strategy quotes remain visible.
        bids = dict(sorted(projection.bid_heatmap.items(), reverse=True)[:HEATMAP_LEVELS_MAX])
        asks = dict(sorted(projection.ask_heatmap.items())[:HEATMAP_LEVELS_MAX])

        self.sequence += 1
        column = {
            "seq": self.sequence,
            "t": timestamp_ms,
            "center": center,
            "bestBid": best_bid_index,
            "bestAsk": best_ask_index,
            "mid": float(projection.mid),
            "bids": {str(key): round(value, 2) for key, value in bids.items()},
            "asks": {str(key): round(value, 2) for key, value in asks.items()},
        }
        self.columns.append(column)
        return column

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.columns)

    def coverage_ms(self) -> int:
        if len(self.columns) < 2:
            return 0
        return max(0, int(self.columns[-1]["t"]) - int(self.columns[0]["t"]))

    def approximate_bytes(self) -> int:
        """粗略内存占用，用于健康检查里公开短窗成本。"""

        total = 0
        for column in self.columns:
            total += 96
            total += 32 * (len(column.get("bids", ())) + len(column.get("asks", ())))
        return total


class TradeBubbleAggregator:
    """逐条保留上游成交事件的有界显示队列。

    ``bucket_ms`` 只用于延迟发布仍可能乱序到达的事件，绝不再按时间、价格
    或方向把多个事件相加。这样页面的最小金额筛选不会把多笔小事件伪装成
    一笔大单；上游 Binance 自身的 aggTrade 聚合语义仍由字段明确披露。
    """

    def __init__(self, *, bucket_ms: int, max_pending: int = 4000) -> None:
        if bucket_ms <= 0 or max_pending <= 0:
            raise ValueError("气泡时间桶与待发布上限必须大于 0")
        self.bucket_ms = bucket_ms
        self.max_pending = max_pending
        self._open: deque[dict[str, Any]] = deque()
        self._forced: deque[dict[str, Any]] = deque(maxlen=max_pending)
        self._ready: deque[dict[str, Any]] = deque(maxlen=max_pending)
        self.dropped = 0

    def reset(self) -> None:
        self._open.clear()
        self._forced.clear()
        self._ready.clear()
        self.dropped = 0

    def rebucket(self, grid: StablePercentGrid) -> None:
        """网格 epoch 变化时逐事件重算价格桶，不合并任何事件。"""

        def normalized(entry: dict[str, Any]) -> dict[str, Any] | None:
            try:
                price = Decimal(str(entry["price"]))
                if not price.is_finite() or price <= 0:
                    return None
                bucket = grid.bucket_index(price)
            except (KeyError, TypeError, ValueError):
                return None
            result = dict(entry)
            result["bucket"] = bucket
            return result

        remapped_open: deque[dict[str, Any]] = deque()
        for raw in self._open:
            entry = normalized(raw)
            if entry is not None:
                remapped_open.append(entry)
        self._open = remapped_open

        remapped_forced: deque[dict[str, Any]] = deque(maxlen=self.max_pending)
        for raw in self._forced:
            entry = normalized(raw)
            if entry is not None:
                remapped_forced.append(entry)
        self._forced = remapped_forced

        remapped_ready: list[dict[str, Any]] = []
        for raw in self._ready:
            entry = normalized(raw)
            if entry is not None:
                remapped_ready.append(entry)
        ready = sorted(
            remapped_ready,
            key=lambda item: (int(item["tradeTime"]), int(item["aggregateId"])),
        )
        self._ready = deque(ready[-self.max_pending :], maxlen=self.max_pending)

    def add(
        self,
        *,
        aggregate_id: int,
        event_time_ms: int,
        first_trade_id: int,
        last_trade_id: int,
        trade_time_ms: int,
        price: Decimal,
        notional: Decimal,
        quantity: Decimal,
        side: str,
        bucket: int,
        source_event_type: str = "aggTrade",
        source_fill_count: int | None = None,
    ) -> None:
        if notional < MIN_DISPLAY_AGGTRADE_NOTIONAL:
            return
        if len(self._open) >= self.max_pending:
            if len(self._forced) == self._forced.maxlen:
                self.dropped += 1
            self._forced.append(self._open.popleft())
        self._open.append(
            {
                "eventTime": event_time_ms,
                "tradeTime": trade_time_ms,
                "id": aggregate_id,
                "aggregateId": aggregate_id,
                "firstTradeId": first_trade_id,
                "lastTradeId": last_trade_id,
                "sourceFillCount": (
                    last_trade_id - first_trade_id + 1
                    if source_fill_count is None
                    else source_fill_count
                ),
                "sourceEventType": source_event_type,
                "applicationAggregation": False,
                "bucket": bucket,
                "side": side,
                "notionalValue": float(notional),
                "quantity": format(quantity, "f"),
                "price": format(price, "f"),
            }
        )

    def _append_ready(self, item: dict[str, Any]) -> None:
        if len(self._ready) == self._ready.maxlen:
            self.dropped += 1
        self._ready.append(item)

    def flush(self, now_ms_value: int) -> list[dict[str, Any]]:
        """发布超过乱序保护窗口的逐事件气泡。"""

        cutoff = now_ms_value - self.bucket_ms
        closed: list[dict[str, Any]] = list(self._forced)
        self._forced.clear()
        pending: deque[dict[str, Any]] = deque()
        while self._open:
            item = self._open.popleft()
            if int(item["tradeTime"]) <= cutoff:
                closed.append(item)
            else:
                pending.append(item)
        self._open = pending
        closed.sort(
            key=lambda item: (
                int(item["tradeTime"]),
                int(item["aggregateId"]),
            )
        )
        for item in closed:
            self._append_ready(item)
        return closed

    def recent(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        items = list(self._ready)
        return items[-limit:]

    def prune(self, cutoff_ms: int) -> None:
        while self._ready and int(self._ready[0]["tradeTime"]) < cutoff_ms:
            self._ready.popleft()
