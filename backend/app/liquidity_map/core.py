"""轻量版核心：Binance USDⓈ-M 只读订单簿与百分比网格聚合。

本模块的 `LocalOrderBook` / `StablePercentGrid` / `ViewAggregator` 语义移植自
`统一交易模块/dom/domtas_app.py`，保持同一套聚合口径（名义金额、trusted 窗口、
守恒字段），避免本项目出现第二套互相矛盾的 DOM 聚合。

移植时按轻量版边界做了删减：不含 SQLite 大单常驻、不含多币历史缓存或磁盘历史。
运行时可按页面创建有界、空闲即停的品种 Hub；本模块本身只读公共行情，不读取
API key，不含账户或交易接口。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from decimal import (
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
    localcontext,
)
from types import MappingProxyType
from typing import Any, Mapping

SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,30}$")

# Binance USDⓈ-M：crypto 永续 + TradFi 永续共用 fapi；交割合约仍排除。
SUPPORTED_UM_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
_KNOWN_QUOTE_SUFFIXES = ("USDT", "USDC", "BUSD", "USD")

SNAPSHOT_LIMIT = 1000
MAX_BOOK_LEVELS_PER_SIDE = 5000
# 单边剩余可信跨度 < 原跨度 × 该比例时重拉快照（越大越早刷新）。
# 0.65 ≈ 用掉约 35% 单边窗口就重居中；比 0.50 更早，但远未到「每跳必刷」。
SNAPSHOT_REFRESH_RATIO = Decimal("0.65")
# mid 相对快照中点漂移 ≥ 整窗宽度 × 该比例时也重拉（补单边判定漏掉的居中漂移）。
SNAPSHOT_MID_DRIFT_RATIO = Decimal("0.35")
MAX_SYNTHETIC_LEVELS_PER_SIDE = 120
MAX_PUBLIC_JSON_BYTES = 8 * 1024 * 1024

DEFAULT_SPACING_PCT = Decimal("0")
DEFAULT_RANGE_PCT = Decimal("1.0")
RANGE_MODE_FIXED = "fixed"
RANGE_MODE_ALL = "all"
RANGE_MODES = frozenset((RANGE_MODE_FIXED, RANGE_MODE_ALL))
# 轻量版默认拉满当前 snapshot 可信窗口，不再提供固定百分比范围选项。
DEFAULT_RANGE_MODE = RANGE_MODE_ALL
# 前端纵轴默认覆盖：(最高价/最低价 - 1) ≈ 5%。
DEFAULT_VIEW_COVERAGE_PCT = Decimal("5")

TRADE_RETENTION_MS = 60_000
MAX_TRADES = 20_000


def normalize_um_symbol(symbol: str) -> str:
    """规范化用户输入：去分隔符、大写；无报价后缀时默认补 USDT。"""

    text = str(symbol or "").strip().upper()
    for sep in ("-", "/", "_", " ", ":"):
        text = text.replace(sep, "")
    if text and not any(text.endswith(suffix) for suffix in _KNOWN_QUOTE_SUFFIXES):
        text = f"{text}USDT"
    return text


def is_supported_um_contract(contract_type: str) -> bool:
    return str(contract_type or "").upper() in SUPPORTED_UM_CONTRACT_TYPES


class FeedError(RuntimeError):
    """公开行情数据不满足本工具的最小契约。"""


class SequenceGap(FeedError):
    """订单簿增量出现缺口，必须从快照重新同步。"""


class SnapshotWindowShift(FeedError):
    """价格已靠近旧 snapshot 边界，需要把可信窗口移回当前盘口。"""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def to_decimal(
    value: Any,
    *,
    positive: bool = False,
    non_negative: bool = False,
) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FeedError(f"无效数字：{value!r}") from exc
    if not result.is_finite():
        raise FeedError(f"数字超出有效范围：{value!r}")
    if positive and result <= 0:
        raise FeedError(f"数字超出有效范围：{value!r}")
    if non_negative and result < 0:
        raise FeedError(f"数字超出有效范围：{value!r}")
    return result


def decimal_text(value: Decimal, *, decimals: int | None = None) -> str:
    """输出不使用科学计数法的权威十进制文本。

    传入 decimals 时按固定小数位补齐尾零（展示用，例如 16.80）；
    不传时仍去掉尾零，适合内部守恒比对。
    """

    if decimals is not None:
        quant = Decimal(1).scaleb(-max(0, int(decimals)))
        return format(value.quantize(quant, rounding=ROUND_HALF_EVEN), "f")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def decimal_places(step: Decimal) -> int:
    """按 tickSize 计算展示小数位；保留尾零语义（0.01000 → 2）。"""

    normalized = step.normalize()
    exponent = normalized.as_tuple().exponent
    if not isinstance(exponent, int):
        return 0
    return max(0, -exponent)


def compact_error(exc: BaseException) -> str:
    text = " ".join(str(exc).strip().split())
    return text[:240] or exc.__class__.__name__


def stdlib_public_json(url: str, user_agent: str) -> Any:
    """aiohttp DNS 临时失败时的独立公共 REST 后备通道。"""

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": user_agent},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=8.0) as response:
        body = response.read(MAX_PUBLIC_JSON_BYTES + 1)
    if len(body) > MAX_PUBLIC_JSON_BYTES:
        raise FeedError("Binance 公共 REST 响应过大")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedError("Binance 公共 REST JSON 无效") from exc


@dataclass(frozen=True)
class SymbolMeta:
    symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    tick_size: Decimal
    price_decimals: int
    quantity_decimals: int
    # 复合市场身份必须由服务端显式提供，不能由前端从 symbol 猜测。
    # 默认值保持既有 Binance 单市场调用与测试兼容。
    target: str = "binance_usdm"
    venue_label: str = "Binance USDⓈ-M"
    dex: str = ""
    collateral_token: str = ""
    is_delisted: bool = False


@dataclass(frozen=True)
class DepthEvent:
    symbol: str
    first_id: int
    final_id: int
    previous_id: int
    event_time: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True)
class Trade:
    sequence: int
    aggregate_id: int
    first_trade_id: int
    last_trade_id: int
    event_time: int
    trade_time: int
    price: Decimal
    quantity: Decimal
    notional: Decimal
    side: str
    # 每条对象仍严格对应一条上游成交事件。Binance 使用 aggTrade；
    # Hyperliquid 使用 trades 订阅中的单条 WsTrade，不在本地做任何聚合。
    source_event_type: str = "aggTrade"
    source_fill_count: int | None = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "id": self.aggregate_id,
            "aggregateId": self.aggregate_id,
            "eventTime": self.event_time,
            "tradeTime": self.trade_time,
            # 保留 Decimal 原始小数位，前端再按 tickSize 固定补齐展示。
            "price": format(self.price, "f"),
            "quantity": format(self.quantity, "f"),
            "notionalValue": float(self.notional),
            "side": self.side,
            "firstTradeId": self.first_trade_id,
            "lastTradeId": self.last_trade_id,
            "sourceFillCount": (
                self.last_trade_id - self.first_trade_id + 1
                if self.source_fill_count is None
                else self.source_fill_count
            ),
            "sourceEventType": self.source_event_type,
        }


class TradeBuffer:
    """按时间和数量双重限制的成交环形缓存；关进程即丢，不落盘。"""

    def __init__(
        self,
        max_items: int = MAX_TRADES,
        retention_ms: int = TRADE_RETENTION_MS,
    ) -> None:
        if max_items <= 0 or retention_ms <= 0:
            raise ValueError("成交缓存限制必须大于 0")
        self.max_items = max_items
        self.retention_ms = retention_ms
        self._items: deque[Trade] = deque()

    def append(self, trade: Trade) -> None:
        self._items.append(trade)
        self.prune(trade.trade_time)

    def prune(self, reference_ms: int) -> None:
        cutoff = reference_ms - self.retention_ms
        while self._items and (
            len(self._items) > self.max_items
            or self._items[0].trade_time < cutoff
        ):
            self._items.popleft()

    def recent(self, limit: int) -> list[Trade]:
        if limit <= 0:
            return []
        items = list(self._items)
        return items[-limit:]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)

    @property
    def coverage_ms(self) -> int:
        if len(self._items) < 2:
            return 0
        return max(0, self._items[-1].trade_time - self._items[0].trade_time)


def parse_depth_event(payload: Any, expected_symbol: str) -> DepthEvent:
    if not isinstance(payload, dict):
        raise FeedError("depth 消息不是 JSON 对象")
    if payload.get("e") != "depthUpdate":
        raise FeedError("收到非 depthUpdate 消息")
    symbol = str(payload.get("s") or "").upper()
    if symbol != expected_symbol:
        raise FeedError(f"depth symbol 不匹配：{symbol}")
    try:
        first_id = int(payload["U"])
        final_id = int(payload["u"])
        previous_id = int(payload["pu"])
        event_time = int(payload.get("E") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedError("depth update ID 无效") from exc
    if first_id < 0 or final_id < first_id or previous_id < 0:
        raise FeedError("depth update ID 范围无效")

    def parse_side(name: str) -> tuple[tuple[Decimal, Decimal], ...]:
        raw = payload.get(name)
        if not isinstance(raw, list):
            raise FeedError(f"depth {name} 不是数组")
        result: list[tuple[Decimal, Decimal]] = []
        for row in raw:
            if not isinstance(row, list) or len(row) < 2:
                raise FeedError(f"depth {name} 档位格式无效")
            price = to_decimal(row[0], positive=True)
            quantity = to_decimal(row[1])
            if quantity < 0:
                raise FeedError("depth quantity 不能为负")
            result.append((price, quantity))
        return tuple(result)

    return DepthEvent(
        symbol=symbol,
        first_id=first_id,
        final_id=final_id,
        previous_id=previous_id,
        event_time=event_time,
        bids=parse_side("b"),
        asks=parse_side("a"),
    )


def parse_trade(payload: Any, expected_symbol: str, sequence: int) -> Trade:
    if not isinstance(payload, dict) or payload.get("e") != "aggTrade":
        raise FeedError("收到非 aggTrade 消息")
    symbol = str(payload.get("s") or "").upper()
    if symbol != expected_symbol:
        raise FeedError(f"aggTrade symbol 不匹配：{symbol}")
    try:
        aggregate_id = int(payload["a"])
        first_trade_id = int(payload["f"])
        last_trade_id = int(payload["l"])
        event_time = int(payload["E"])
        trade_time = int(payload["T"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedError("aggTrade 标识或时间无效") from exc
    if (
        aggregate_id < 0
        or first_trade_id < 0
        or last_trade_id < first_trade_id
        or event_time < 0
        or trade_time < 0
    ):
        raise FeedError("aggTrade 标识或时间范围无效")
    price = to_decimal(payload.get("p"), positive=True)
    quantity = to_decimal(payload.get("q"), positive=True)
    # m=true 表示 buyer is maker，因此 taker 是主动卖方。
    side = "sell" if payload.get("m") is True else "buy"
    return Trade(
        sequence=sequence,
        aggregate_id=aggregate_id,
        first_trade_id=first_trade_id,
        last_trade_id=last_trade_id,
        event_time=event_time,
        trade_time=trade_time,
        price=price,
        quantity=quantity,
        notional=price * quantity,
        side=side,
    )


class LocalOrderBook:
    """用 Decimal 保存绝对数量的 Binance 本地订单簿。"""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.snapshot_bid_floor: Decimal | None = None
        self.snapshot_ask_ceiling: Decimal | None = None
        self.snapshot_bid_complete = False
        self.snapshot_ask_complete = False
        self.snapshot_bid_span = Decimal(0)
        self.snapshot_ask_span = Decimal(0)
        self.snapshot_mid: Decimal | None = None
        self.snapshot_window_width = Decimal(0)
        # REST snapshot + diff 的可信边界在一次同步周期内稳定；Hyperliquid
        # l2Book 则每条消息原子替换当前可见 20 档，边界天然会随挂撤单移动。
        # 聚合网格必须区分这两种语义，不能把动态可见边界当成网格身份。
        self.snapshot_bounds_dynamic = False
        self.source_depth_limit = SNAPSHOT_LIMIT
        self.last_update_id: int | None = None
        self.ready = False
        self.revision = 0

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.snapshot_bid_floor = None
        self.snapshot_ask_ceiling = None
        self.snapshot_bid_complete = False
        self.snapshot_ask_complete = False
        self.snapshot_bid_span = Decimal(0)
        self.snapshot_ask_span = Decimal(0)
        self.snapshot_mid = None
        self.snapshot_window_width = Decimal(0)
        self.snapshot_bounds_dynamic = False
        self.source_depth_limit = SNAPSHOT_LIMIT
        self.last_update_id = None
        self.ready = False
        self.revision += 1

    def load_snapshot(self, payload: Any) -> int:
        if not isinstance(payload, dict):
            raise FeedError("depth snapshot 不是 JSON 对象")
        try:
            update_id = int(payload["lastUpdateId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError("depth snapshot 缺少 lastUpdateId") from exc
        if update_id < 0:
            raise FeedError("depth snapshot lastUpdateId 无效")

        def parse_rows(name: str) -> dict[Decimal, Decimal]:
            rows = payload.get(name)
            if not isinstance(rows, list) or len(rows) > SNAPSHOT_LIMIT:
                raise FeedError(f"depth snapshot {name} 无效")
            result: dict[Decimal, Decimal] = {}
            for row in rows:
                if not isinstance(row, list) or len(row) < 2:
                    raise FeedError(f"depth snapshot {name} 档位无效")
                price = to_decimal(row[0], positive=True)
                quantity = to_decimal(row[1])
                if quantity < 0:
                    raise FeedError("snapshot quantity 不能为负")
                if quantity:
                    result[price] = quantity
            return result

        self.bids = parse_rows("bids")
        self.asks = parse_rows("asks")
        self.snapshot_bid_floor = min(self.bids) if self.bids else None
        self.snapshot_ask_ceiling = max(self.asks) if self.asks else None
        self.snapshot_bid_complete = len(payload["bids"]) < SNAPSHOT_LIMIT
        self.snapshot_ask_complete = len(payload["asks"]) < SNAPSHOT_LIMIT
        self.snapshot_bid_span = (
            max(self.bids) - self.snapshot_bid_floor
            if self.bids and self.snapshot_bid_floor is not None
            else Decimal(0)
        )
        self.snapshot_ask_span = (
            self.snapshot_ask_ceiling - min(self.asks)
            if self.asks and self.snapshot_ask_ceiling is not None
            else Decimal(0)
        )
        best_bid = max(self.bids) if self.bids else None
        best_ask = min(self.asks) if self.asks else None
        self.snapshot_mid = (
            (best_bid + best_ask) / 2
            if best_bid is not None and best_ask is not None
            else None
        )
        self.snapshot_window_width = (
            self.snapshot_ask_ceiling - self.snapshot_bid_floor
            if self.snapshot_bid_floor is not None
            and self.snapshot_ask_ceiling is not None
            else Decimal(0)
        )
        self.snapshot_bounds_dynamic = False
        self.last_update_id = update_id
        self.ready = False
        self._assert_not_crossed()
        self.revision += 1
        return update_id

    def replace_snapshot(
        self,
        bids: Any,
        asks: Any,
        update_id: int,
        *,
        limit: int = SNAPSHOT_LIMIT,
    ) -> int:
        """原子替换一个不带增量序号的完整可见订单簿。

        Hyperliquid ``l2Book`` 每次推送当前 20 档快照，没有 Binance 的
        ``U/u/pu`` 序列；因此不能伪造 diff bridge。这里按消息时间防倒退，
        校验两侧后一次替换，并立即把该可见快照标记为 ready。
        """

        try:
            parsed_update_id = int(update_id)
            parsed_limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise FeedError("snapshot update_id 或 limit 无效") from exc
        if parsed_update_id < 0 or not 1 <= parsed_limit <= MAX_BOOK_LEVELS_PER_SIDE:
            raise FeedError("snapshot update_id 或 limit 超出范围")
        if self.last_update_id is not None and parsed_update_id < self.last_update_id:
            return self.last_update_id

        def parse_rows(name: str, rows: Any) -> dict[Decimal, Decimal]:
            if not isinstance(rows, (list, tuple)) or len(rows) > parsed_limit:
                raise FeedError(f"snapshot {name} 无效")
            result: dict[Decimal, Decimal] = {}
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    raise FeedError(f"snapshot {name} 档位无效")
                price = to_decimal(row[0], positive=True)
                quantity = to_decimal(row[1], non_negative=True)
                if quantity:
                    result[price] = quantity
            return result

        next_bids = parse_rows("bids", bids)
        next_asks = parse_rows("asks", asks)
        if not next_bids or not next_asks:
            raise FeedError("snapshot 至少需要一档买卖盘")
        if max(next_bids) >= min(next_asks):
            raise SequenceGap("快照订单簿出现 crossed book")

        self.bids = next_bids
        self.asks = next_asks
        self.snapshot_bid_floor = min(next_bids)
        self.snapshot_ask_ceiling = max(next_asks)
        # 这是上游返回的完整「可见窗口」快照，不代表完整价格域。明确保持
        # sourceComplete=false，避免把 20 档边界外的流动性误解释成可信的 0。
        # Hyperliquid feed 不调用 Binance 专属的 snapshot window refresh。
        self.snapshot_bid_complete = False
        self.snapshot_ask_complete = False
        self.snapshot_bid_span = max(next_bids) - self.snapshot_bid_floor
        self.snapshot_ask_span = self.snapshot_ask_ceiling - min(next_asks)
        best_bid = max(next_bids)
        best_ask = min(next_asks)
        self.snapshot_mid = (best_bid + best_ask) / Decimal(2)
        self.snapshot_window_width = (
            self.snapshot_ask_ceiling - self.snapshot_bid_floor
        )
        self.snapshot_bounds_dynamic = True
        self.source_depth_limit = parsed_limit
        self.last_update_id = parsed_update_id
        self.ready = True
        self.revision += 1
        return parsed_update_id

    def bridge(self, event: DepthEvent) -> bool:
        if self.last_update_id is None:
            raise FeedError("不能在快照前桥接 depth")
        snapshot_id = self.last_update_id
        if event.final_id < snapshot_id:
            return False
        if not (event.first_id <= snapshot_id <= event.final_id):
            if event.first_id > snapshot_id:
                raise SequenceGap(
                    f"快照与首个增量未桥接：U={event.first_id}, "
                    f"L={snapshot_id}, u={event.final_id}"
                )
            return False
        self._apply_levels(event)
        self.last_update_id = event.final_id
        self.ready = True
        self._assert_not_crossed()
        self._trim()
        self.revision += 1
        return True

    def apply(self, event: DepthEvent) -> bool:
        if not self.ready or self.last_update_id is None:
            raise FeedError("订单簿尚未桥接")
        if event.final_id <= self.last_update_id:
            return False
        if event.previous_id != self.last_update_id:
            raise SequenceGap(
                f"pu 缺口：expected={self.last_update_id}, "
                f"actual={event.previous_id}"
            )
        self._apply_levels(event)
        self.last_update_id = event.final_id
        self._assert_not_crossed()
        self._trim()
        self.revision += 1
        return True

    def _apply_levels(self, event: DepthEvent) -> None:
        for target, rows in ((self.bids, event.bids), (self.asks, event.asks)):
            for price, quantity in rows:
                if quantity == 0:
                    target.pop(price, None)
                else:
                    target[price] = quantity

    def _assert_not_crossed(self) -> None:
        if not self.bids or not self.asks:
            return
        if max(self.bids) >= min(self.asks):
            self.reset()
            raise SequenceGap("本地订单簿出现 crossed book")

    def _trim(self) -> None:
        # Binance REST 快照只证明当前 source_depth_limit 档窗口。diff 流长期
        # 运行时会不断触及窗外价位；若把这些零散更新无限累积成“更深盘口”，
        # 既不完整，也会让每秒投影从 1000 档膨胀到数千档。保留离盘口最近的
        # 同等档数，并留 10% 回差，避免每个新增价位都重新排序。
        limit = max(1, min(MAX_BOOK_LEVELS_PER_SIDE, self.source_depth_limit))
        trim_at = limit + max(16, limit // 10)
        if len(self.bids) > trim_at:
            keep = sorted(self.bids, reverse=True)[:limit]
            self.bids = {price: self.bids[price] for price in keep}
        if len(self.asks) > trim_at:
            keep = sorted(self.asks)[:limit]
            self.asks = {price: self.asks[price] for price in keep}

    def trusted_window_needs_refresh(self) -> bool:
        """价格靠近旧快照外缘或 mid 漂移过大时主动重取，把可信窗移回当前盘口。

        不能突破 Binance `depth?limit=1000` 的物理深度；刷新只是重新居中，
        不会变出更厚的簿。受控刷新会写热图 gap 列（fail-closed）。
        """

        best_bid = self.best_bid
        best_ask = self.best_ask
        if best_bid is None or best_ask is None:
            return False
        if (
            not self.snapshot_bid_complete
            and self.snapshot_bid_floor is not None
            and self.snapshot_bid_span > 0
            and best_bid - self.snapshot_bid_floor
            < self.snapshot_bid_span * SNAPSHOT_REFRESH_RATIO
        ):
            return True
        if (
            not self.snapshot_ask_complete
            and self.snapshot_ask_ceiling is not None
            and self.snapshot_ask_span > 0
            and self.snapshot_ask_ceiling - best_ask
            < self.snapshot_ask_span * SNAPSHOT_REFRESH_RATIO
        ):
            return True
        mid = self.mid
        if (
            mid is not None
            and self.snapshot_mid is not None
            and self.snapshot_window_width > 0
            and abs(mid - self.snapshot_mid)
            >= self.snapshot_window_width * SNAPSHOT_MID_DRIFT_RATIO
        ):
            return True
        return False

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        best_bid = self.best_bid
        best_ask = self.best_ask
        if best_bid is None or best_ask is None:
            return None
        return (best_bid + best_ask) / Decimal(2)


class StablePercentGrid:
    """锚定式乘法网格。

    anchor 和 ratio 在 symbol/间距/范围不变时保持不动。mid 只有跨过完整
    bucket 边界才会改变 center index，因此微小价格变化不会重建全部边界。
    热图列与右侧 DOM 共用同一套 bucket index，时间轴上因此天然对齐。
    """

    def __init__(
        self,
        *,
        anchor: Decimal,
        requested_spacing_pct: Decimal,
        range_pct: Decimal,
        tick_size: Decimal,
        fit_lower: Decimal | None = None,
        fit_upper: Decimal | None = None,
    ) -> None:
        if anchor <= 0 or tick_size <= 0:
            raise ValueError("anchor 和 tick_size 必须大于 0")
        if requested_spacing_pct < 0 or requested_spacing_pct > Decimal("5"):
            raise ValueError("spacing 超出范围")
        # 0 = 不按百分比聚合，有效宽度压到恰好覆盖一 tick。
        requested_fraction = (
            Decimal(0)
            if requested_spacing_pct == 0
            else requested_spacing_pct / Decimal(100)
        )
        if requested_fraction > Decimal("0.05"):
            raise ValueError("spacing 超出范围")
        if fit_lower is not None or fit_upper is not None:
            if (
                fit_lower is None
                or fit_upper is None
                or not Decimal(0) < fit_lower < anchor < fit_upper
            ):
                raise ValueError("全部深度边界无效")
            low_reference = fit_lower
            with localcontext() as ctx:
                ctx.prec = 50
                bid_fit_fraction = (
                    ((anchor / fit_lower).ln() / MAX_SYNTHETIC_LEVELS_PER_SIDE).exp()
                    - Decimal(1)
                )
                ask_fit_fraction = (
                    ((fit_upper / anchor).ln() / MAX_SYNTHETIC_LEVELS_PER_SIDE).exp()
                    - Decimal(1)
                )
            if requested_spacing_pct == 0:
                # 正常窄盘口仍沿用整本可信下界的一 tick 分辨率；当异常远单低于
                # 当前价一半时，只允许它把显示参考拉到 anchor/2。这样 AEVO 的
                # minPrice 远端单不会把档距推成 100%，同时又避免所有币都严格按
                # 当前价一 tick 建网格而放大 3 分钟 ring、网络帧和 CPU。
                # 全深度行改为稀疏输出，阈值外真实订单及金额守恒仍完整保留。
                display_low_reference = max(fit_lower, anchor / Decimal(2))
                effective_fraction = (
                    tick_size
                    / display_low_reference
                    * Decimal("1.000000000001")
                )
            else:
                tick_floor = (
                    tick_size / low_reference * Decimal("1.000000000001")
                )
                effective_fraction = max(
                    requested_fraction,
                    tick_floor,
                    bid_fit_fraction,
                    ask_fit_fraction,
                )
        else:
            range_fraction = range_pct / Decimal(100)
            if not (Decimal("0.001") <= range_fraction < Decimal("0.5")):
                raise ValueError("range 超出范围")
            low_reference = anchor * (Decimal(1) - range_fraction)
            effective_fraction = max(
                requested_fraction,
                tick_size / low_reference,
            )
            # count 使用 ceil，最低可视桶会略超出用户目标范围。迭代到该桶的
            # 实际乘法宽度也不小于一个原生 tick，避免百分比档小于交易所一跳。
            with localcontext() as ctx:
                ctx.prec = 50
                for _ in range(12):
                    ratio = Decimal(1) + effective_fraction
                    log_ratio = ratio.ln()
                    count = int(
                        (
                            -(Decimal(1) - range_fraction).ln() / log_ratio
                        ).to_integral_value(rounding=ROUND_CEILING)
                    )
                    count = max(1, count)
                    lowest_boundary = anchor * (ratio ** (-count))
                    required = (
                        tick_size / lowest_boundary * Decimal("1.000000000001")
                    )
                    if required <= effective_fraction:
                        break
                    effective_fraction = required
        self.effective_fraction = effective_fraction
        self.requested_spacing_pct = requested_spacing_pct
        self.range_pct = range_pct
        self.tick_size = tick_size
        self.anchor = anchor
        self.ratio = Decimal(1) + self.effective_fraction
        with localcontext() as ctx:
            ctx.prec = 50
            self.log_ratio = self.ratio.ln()
        self._boundary_cache: dict[int, Decimal] = {0: anchor}
        self._index_cache: dict[Decimal, int] = {}

    @property
    def effective_spacing_pct(self) -> Decimal:
        return self.effective_fraction * Decimal(100)

    def boundary(self, index: int) -> Decimal:
        cached = self._boundary_cache.get(index)
        if cached is not None:
            return cached
        with localcontext() as ctx:
            ctx.prec = 50
            value = self.anchor * (self.ratio**index)
        if len(self._boundary_cache) < 4000:
            self._boundary_cache[index] = value
        return value

    def bucket_index(self, price: Decimal) -> int:
        cached = self._index_cache.get(price)
        if cached is not None:
            return cached
        if price <= 0:
            raise ValueError("price 必须大于 0")
        with localcontext() as ctx:
            ctx.prec = 50
            estimate = (price / self.anchor).ln() / self.log_ratio
            index = int(estimate.to_integral_value(rounding=ROUND_FLOOR))
        # Decimal 的 ln 是高精度近似；用真实边界二次校正，避免恰好落边界时错档。
        while price < self.boundary(index):
            index -= 1
        while price >= self.boundary(index + 1):
            index += 1
        if len(self._index_cache) >= 12_000:
            self._index_cache.clear()
        self._index_cache[price] = index
        return index

    def visible_count(self) -> int:
        fraction = self.range_pct / Decimal(100)
        with localcontext() as ctx:
            ctx.prec = 50
            ask_count = (
                (Decimal(1) + fraction).ln() / self.log_ratio
            ).to_integral_value(rounding=ROUND_CEILING)
            bid_count = (
                -(Decimal(1) - fraction).ln() / self.log_ratio
            ).to_integral_value(rounding=ROUND_CEILING)
        return max(
            1,
            min(MAX_SYNTHETIC_LEVELS_PER_SIDE, int(max(ask_count, bid_count))),
        )


@dataclass(frozen=True, slots=True)
class GridProjection:
    """一个订单簿 revision 在固定价格网格上的未裁剪投影。

    DOM 与热图对可信边界的解释不同，因此这里故意保存整本订单簿的 bucket
    投影，不预先套用 DOM 的 snapshot 可信窗，也不套用热图的极端距离保护。
    两个消费者只共享昂贵的 ``price -> bucket`` 遍历，仍各自执行原有裁剪。

    ``*_notional`` 保留 Decimal，供 DOM 守恒与展示使用；``*_heatmap`` 同时
    保留旧热图逐原生价位转 float 后累加的数值口径，避免共享投影改变列值。
    所有映射均为只读 proxy，可以安全地在同一事件循环的多个消费者间复用。
    """

    book: LocalOrderBook
    symbol: str
    book_revision: int
    grid: StablePercentGrid
    grid_epoch: int
    center: int
    best_bid: int
    best_ask: int
    best_bid_price: Decimal
    best_ask_price: Decimal
    mid: Decimal
    bid_notional: Mapping[int, Decimal]
    ask_notional: Mapping[int, Decimal]
    bid_heatmap: Mapping[int, float]
    ask_heatmap: Mapping[int, float]
    bid_levels: Mapping[int, int]
    ask_levels: Mapping[int, int]


def project_order_book(
    book: LocalOrderBook,
    grid: StablePercentGrid,
    grid_epoch: int,
    *,
    best_bid: Decimal | None = None,
    best_ask: Decimal | None = None,
) -> GridProjection | None:
    """把当前 ready 订单簿完整投影到一个网格，不施加消费者裁剪。

    调用期间不能 ``await``，也不能放到会与 feed 并发修改 ``book`` 的线程；
    ``book_revision`` 是缓存/失效键，不是跨线程读取的一致性锁。
    """

    if not book.ready:
        return None
    if best_bid is None:
        best_bid = book.best_bid
    if best_ask is None:
        best_ask = book.best_ask
    if best_bid is None or best_ask is None:
        return None
    mid = (best_bid + best_ask) / Decimal(2)
    center = grid.bucket_index(mid)
    best_bid_index = grid.bucket_index(best_bid)
    best_ask_index = grid.bucket_index(best_ask)

    bid_notional: dict[int, Decimal] = {}
    ask_notional: dict[int, Decimal] = {}
    bid_heatmap: dict[int, float] = {}
    ask_heatmap: dict[int, float] = {}
    bid_levels: dict[int, int] = {}
    ask_levels: dict[int, int] = {}
    for price, quantity in book.bids.items():
        index = grid.bucket_index(price)
        notional = price * quantity
        bid_notional[index] = bid_notional.get(index, Decimal(0)) + notional
        bid_heatmap[index] = bid_heatmap.get(index, 0.0) + float(notional)
        bid_levels[index] = bid_levels.get(index, 0) + 1
    for price, quantity in book.asks.items():
        index = grid.bucket_index(price)
        notional = price * quantity
        ask_notional[index] = ask_notional.get(index, Decimal(0)) + notional
        ask_heatmap[index] = ask_heatmap.get(index, 0.0) + float(notional)
        ask_levels[index] = ask_levels.get(index, 0) + 1

    return GridProjection(
        book=book,
        symbol=book.symbol,
        book_revision=book.revision,
        grid=grid,
        grid_epoch=int(grid_epoch),
        center=center,
        best_bid=best_bid_index,
        best_ask=best_ask_index,
        best_bid_price=best_bid,
        best_ask_price=best_ask,
        mid=mid,
        bid_notional=MappingProxyType(bid_notional),
        ask_notional=MappingProxyType(ask_notional),
        bid_heatmap=MappingProxyType(bid_heatmap),
        ask_heatmap=MappingProxyType(ask_heatmap),
        bid_levels=MappingProxyType(bid_levels),
        ask_levels=MappingProxyType(ask_levels),
    )


class ViewAggregator:
    """为一个视图维护稳定网格和显示参数。

    `grid_epoch` 在网格重建时自增；热图 ring 依赖它判断历史列是否仍可对齐。
    """

    def __init__(
        self,
        spacing_pct: Decimal = DEFAULT_SPACING_PCT,
        range_pct: Decimal = DEFAULT_RANGE_PCT,
        range_mode: str = DEFAULT_RANGE_MODE,
    ) -> None:
        self.spacing_pct = spacing_pct
        self.range_pct = range_pct
        self.range_mode = range_mode
        self.grid: StablePercentGrid | None = None
        self.grid_epoch = 0
        self._grid_signature: tuple[Any, ...] | None = None
        self._projection_cache: GridProjection | None = None

    def configure(
        self,
        spacing_pct: Decimal,
        range_pct: Decimal,
        range_mode: str = DEFAULT_RANGE_MODE,
    ) -> None:
        if (
            spacing_pct != self.spacing_pct
            or range_pct != self.range_pct
            or range_mode != self.range_mode
        ):
            if spacing_pct < 0 or spacing_pct > Decimal("5"):
                raise ValueError("spacing 超出范围")
            self.spacing_pct = spacing_pct
            self.range_pct = range_pct
            self.range_mode = range_mode
            self.grid = None
            self._grid_signature = None
            self._projection_cache = None

    def _ensure_grid(
        self,
        book: LocalOrderBook,
        meta: SymbolMeta,
        mid: Decimal,
    ) -> StablePercentGrid:
        all_depth = self.range_mode == RANGE_MODE_ALL
        fit_lower = (
            book.snapshot_bid_floor or min(book.bids) if all_depth else None
        )
        fit_upper = (
            book.snapshot_ask_ceiling or max(book.asks) if all_depth else None
        )
        signature = (
            meta.symbol,
            meta.tick_size,
            self.spacing_pct,
            self.range_pct,
            self.range_mode,
            None if book.snapshot_bounds_dynamic else fit_lower,
            None if book.snapshot_bounds_dynamic else fit_upper,
            book.snapshot_bounds_dynamic,
            book.snapshot_bid_complete,
            book.snapshot_ask_complete,
        )
        if self.grid is None or signature != self._grid_signature:
            self.grid = StablePercentGrid(
                anchor=mid,
                requested_spacing_pct=self.spacing_pct,
                range_pct=self.range_pct,
                tick_size=meta.tick_size,
                fit_lower=fit_lower,
                fit_upper=fit_upper,
            )
            self._grid_signature = signature
            self.grid_epoch += 1
            self._projection_cache = None
        return self.grid

    def project(
        self,
        book: LocalOrderBook,
        meta: SymbolMeta,
    ) -> GridProjection | None:
        """返回当前 ``book.revision`` 的共享未裁剪投影。

        缓存同时绑定订单簿对象、revision、网格对象与 epoch。活跃 WS 改簿后
        revision 会失效缓存；同一发布 tick 的 DOM/热图则可复用同一对象。
        """

        if not book.ready:
            return None
        best_bid = book.best_bid
        best_ask = book.best_ask
        if best_bid is None or best_ask is None:
            return None
        mid = (best_bid + best_ask) / Decimal(2)
        grid = self._ensure_grid(book, meta, mid)
        cached = self._projection_cache
        if (
            cached is not None
            and cached.book is book
            and cached.symbol == book.symbol
            and cached.book_revision == book.revision
            and cached.grid is grid
            and cached.grid_epoch == self.grid_epoch
        ):
            return cached
        projection = project_order_book(
            book,
            grid,
            self.grid_epoch,
            best_bid=best_bid,
            best_ask=best_ask,
        )
        self._projection_cache = projection
        return projection

    def aggregate(self, book: LocalOrderBook, meta: SymbolMeta) -> dict[str, Any]:
        projection = self.project(book, meta)
        if projection is None:
            return {"ready": False, "bids": [], "asks": [], "gaps": []}
        return self.aggregate_projection(book, meta, projection)

    def aggregate_projection(
        self,
        book: LocalOrderBook,
        meta: SymbolMeta,
        projection: GridProjection,
    ) -> dict[str, Any]:
        """按 DOM 原有可信边界消费共享投影，不改变热图的可见范围。"""

        grid = self.grid
        if (
            grid is None
            or projection.book is not book
            or projection.symbol != book.symbol
            or projection.book_revision != book.revision
            or projection.grid is not grid
            or projection.grid_epoch != self.grid_epoch
        ):
            raise ValueError("订单簿投影已过期或不属于当前网格")
        mid = projection.mid
        best_bid = projection.best_bid_price
        best_ask = projection.best_ask_price
        center = projection.center
        best_bid_index = projection.best_bid
        best_ask_index = projection.best_ask
        all_depth = self.range_mode == RANGE_MODE_ALL
        fit_lower = (
            book.snapshot_bid_floor or min(book.bids) if all_depth else None
        )
        fit_upper = (
            book.snapshot_ask_ceiling or max(book.asks) if all_depth else None
        )
        bid_amounts: dict[int, Decimal] = {}
        ask_amounts: dict[int, Decimal] = {}
        bid_levels: dict[int, int] = {}
        ask_levels: dict[int, int] = {}
        source_bid_total = Decimal(0)
        source_ask_total = Decimal(0)
        if all_depth:
            assert fit_lower is not None and fit_upper is not None
            low_index = grid.bucket_index(fit_lower)
            high_index = grid.bucket_index(fit_upper)
            if not book.snapshot_bid_complete:
                while low_index <= best_bid_index and not self._bid_bucket_trusted(
                    grid, low_index, meta, book
                ):
                    low_index += 1
            if not book.snapshot_ask_complete:
                while high_index >= best_ask_index and not self._ask_bucket_trusted(
                    grid, high_index, meta, book
                ):
                    high_index -= 1
            low_index = min(low_index, best_bid_index)
            high_index = max(high_index, best_ask_index)
            count = max(
                best_bid_index - low_index + 1,
                high_index - best_ask_index + 1,
            )
        else:
            count = grid.visible_count()
            low_index = center - count + 1
            high_index = center + count - 1

        for index, notional in projection.bid_notional.items():
            if low_index <= index <= best_bid_index:
                bid_amounts[index] = notional
                bid_levels[index] = projection.bid_levels[index]
                source_bid_total += notional
        for index, notional in projection.ask_notional.items():
            if best_ask_index <= index <= high_index:
                ask_amounts[index] = notional
                ask_levels[index] = projection.ask_levels[index]
                source_ask_total += notional

        # all-depth 可能含有离当前价非常远的真实挂单。只输出有订单的 bucket，
        # 缺失整数 bucket 本身就代表空档；这既保留完整盘口，又避免极远单把
        # 稠密列表扩成数万行。fixed 模式继续保留原有稠密可视窗口契约。
        bid_indices = (
            sorted(bid_amounts, reverse=True)
            if all_depth
            else list(range(best_bid_index, low_index - 1, -1))
        )
        ask_indices = (
            sorted(ask_amounts)
            if all_depth
            else list(range(best_ask_index, high_index + 1))
        )
        bids = [
            self._row(
                grid,
                index,
                "bid",
                bid_amounts.get(index, Decimal(0)),
                bid_levels.get(index, 0),
                mid,
                best_bid,
                index == best_bid_index,
                meta,
                self._bid_bucket_trusted(grid, index, meta, book),
            )
            for index in bid_indices
        ]
        asks = [
            self._row(
                grid,
                index,
                "ask",
                ask_amounts.get(index, Decimal(0)),
                ask_levels.get(index, 0),
                mid,
                best_ask,
                index == best_ask_index,
                meta,
                self._ask_bucket_trusted(grid, index, meta, book),
            )
            for index in ask_indices
        ]
        gap_low_index = max(low_index, best_bid_index + 1)
        gap_high_index = min(high_index, best_ask_index - 1)
        gaps = (
            []
            if all_depth
            else [
                self._gap_row(grid, index, mid, meta)
                for index in range(gap_low_index, gap_high_index + 1)
            ]
        )

        aggregated_bid_total = self._add_cumulative(bids)
        aggregated_ask_total = self._add_cumulative(asks)
        trusted_bid_rows = [row for row in bids if row["trusted"]]
        trusted_ask_rows = [row for row in asks if row["trusted"]]
        trusted_bid_coverage = (
            abs(Decimal(str(trusted_bid_rows[-1]["farDistancePct"])))
            if trusted_bid_rows
            else Decimal(0)
        )
        trusted_ask_coverage = (
            abs(Decimal(str(trusted_ask_rows[-1]["farDistancePct"])))
            if trusted_ask_rows
            else Decimal(0)
        )
        low_boundary = grid.boundary(low_index)
        high_boundary = grid.boundary(high_index + 1)
        return {
            "ready": True,
            "gridEpoch": self.grid_epoch,
            "centerBucket": center,
            "bestBidBucket": best_bid_index,
            "bestAskBucket": best_ask_index,
            "levelCountPerSide": count,
            "requestedSpacingPct": decimal_text(self.spacing_pct),
            "effectiveSpacingPct": decimal_text(
                grid.effective_spacing_pct.quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_EVEN
                )
            ),
            "rangeMode": self.range_mode,
            "rangePct": decimal_text(self.range_pct) if not all_depth else None,
            "allDepthFitAdjusted": (
                all_depth and grid.effective_spacing_pct > self.spacing_pct
            ),
            "actualLowerCoveragePct": float((mid - low_boundary) / mid * Decimal(100)),
            "actualUpperCoveragePct": float((high_boundary - mid) / mid * Decimal(100)),
            "snapshotDepthLimit": int(
                getattr(book, "source_depth_limit", SNAPSHOT_LIMIT)
            ),
            "trustedBidCoveragePct": float(trusted_bid_coverage),
            "trustedAskCoveragePct": float(trusted_ask_coverage),
            "trustedBidLevelCount": len(trusted_bid_rows),
            "trustedAskLevelCount": len(trusted_ask_rows),
            "bids": bids,
            "asks": asks,
            "gaps": gaps,
            "conservation": {
                "bidSource": decimal_text(source_bid_total),
                "bidAggregated": decimal_text(aggregated_bid_total),
                "askSource": decimal_text(source_ask_total),
                "askAggregated": decimal_text(aggregated_ask_total),
            },
        }

    @staticmethod
    def _bid_bucket_trusted(
        grid: StablePercentGrid,
        index: int,
        meta: SymbolMeta,
        book: LocalOrderBook,
    ) -> bool:
        return book.snapshot_bid_complete or (
            book.snapshot_bid_floor is not None
            and grid.boundary(index).quantize(
                meta.tick_size, rounding=ROUND_CEILING
            )
            >= book.snapshot_bid_floor
        )

    @staticmethod
    def _ask_bucket_trusted(
        grid: StablePercentGrid,
        index: int,
        meta: SymbolMeta,
        book: LocalOrderBook,
    ) -> bool:
        return book.snapshot_ask_complete or (
            book.snapshot_ask_ceiling is not None
            and grid.boundary(index + 1).quantize(
                meta.tick_size, rounding=ROUND_FLOOR
            )
            <= book.snapshot_ask_ceiling
        )

    @staticmethod
    def _row(
        grid: StablePercentGrid,
        index: int,
        side: str,
        notional: Decimal,
        raw_levels: int,
        mid: Decimal,
        best_price: Decimal,
        best: bool,
        meta: SymbolMeta,
        trusted: bool,
    ) -> dict[str, Any]:
        lower = grid.boundary(index)
        upper = grid.boundary(index + 1)
        grid_lower_tick = lower.quantize(meta.tick_size, rounding=ROUND_CEILING)
        grid_upper_tick = upper.quantize(meta.tick_size, rounding=ROUND_FLOOR)
        if grid_upper_tick < grid_lower_tick:
            grid_upper_tick = grid_lower_tick
        lower_tick = grid_lower_tick
        upper_tick = grid_upper_tick
        if best:
            if side == "bid":
                upper_tick = min(upper_tick, best_price)
            else:
                lower_tick = max(lower_tick, best_price)
        if side == "bid":
            near_price = min(upper_tick, mid)
        else:
            near_price = max(lower_tick, mid)
        distance_pct = (near_price - mid) / mid * Decimal(100)
        far_price = lower_tick if side == "bid" else upper_tick
        far_distance_pct = (far_price - best_price) / best_price * Decimal(100)
        return {
            "bucket": index,
            "side": side,
            "lower": decimal_text(lower_tick),
            "upper": decimal_text(upper_tick),
            "distancePct": float(distance_pct),
            "farDistancePct": float(far_distance_pct),
            "notional": decimal_text(notional),
            "notionalValue": float(notional),
            "rawLevels": raw_levels,
            "best": best,
            "trusted": trusted,
        }

    @staticmethod
    def _gap_row(
        grid: StablePercentGrid,
        index: int,
        mid: Decimal,
        meta: SymbolMeta,
    ) -> dict[str, Any]:
        lower = grid.boundary(index)
        upper = grid.boundary(index + 1)
        lower_tick = lower.quantize(meta.tick_size, rounding=ROUND_CEILING)
        upper_tick = upper.quantize(meta.tick_size, rounding=ROUND_FLOOR)
        if upper_tick < lower_tick:
            upper_tick = lower_tick
        midpoint = (lower_tick + upper_tick) / Decimal(2)
        return {
            "bucket": index,
            "side": "gap",
            "lower": decimal_text(lower_tick),
            "upper": decimal_text(upper_tick),
            "distancePct": float((midpoint - mid) / mid * Decimal(100)),
        }

    @staticmethod
    def _add_cumulative(rows: list[dict[str, Any]]) -> Decimal:
        """按 best 向外的顺序写入可视范围累计名义金额。"""

        running = Decimal(0)
        for row in rows:
            running += to_decimal(row["notional"])
            if row["trusted"]:
                row["cumulativeNotionalValue"] = float(running)
            else:
                row["cumulativeNotionalValue"] = None
        return running
