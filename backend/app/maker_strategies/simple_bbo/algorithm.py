"""Four-order BBO mirror and fixed-notional second levels. No ladders, schedules, index feed or FLOW calculations."""
from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import asyncio
import time

from pydantic import BaseModel, ConfigDict, Field

from app.services.contract_ladder_feed import LadderFeed
from app.services.maker_execution import MakerExecutionWorker, D, leaves, side_of


class BBOParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    second_level_notional: Decimal = Field(default=Decimal("1000000"), gt=0, max_digits=20, decimal_places=8)
    second_level_distance_bps: Decimal = Field(default=Decimal("5"), gt=0, lt=10000, max_digits=12, decimal_places=8)


def default_config(symbol="BTCUSDT"):
    return {"enabled": False, "primary_source": {"exchange": "BINANCE", "symbol": symbol},
            **BBOParameters().model_dump(mode="json")}


def validate_config(config, metadata=None):
    legacy = bool(set(config) & {"buy_offset_pct", "sell_offset_pct"})
    if set(config) - {"enabled", "primary_source", "buy_offset_pct", "sell_offset_pct", "second_level_notional", "second_level_distance_bps"}:
        raise ValueError("极简铺单仅支持二档金额、距离与上游配置")
    if type(config.get("enabled")) is not bool:
        raise ValueError("enabled 必须是布尔值")
    source = config.get("primary_source", {})
    symbol = source.get("symbol", "")
    if source.get("exchange") != "BINANCE" or not isinstance(symbol, str) or not symbol.isalnum() or not 2 <= len(symbol) <= 30:
        raise ValueError("必须配置有效的 Binance bookTicker 上游标的")
    params = BBOParameters.model_validate({k: config[k] for k in ("second_level_notional", "second_level_distance_bps") if k in config})
    # Old two-order configurations never silently acquire million-USDT orders on reload.
    return {"enabled": False if legacy else config["enabled"], "primary_source": {"exchange": "BINANCE", "symbol": symbol.upper()},
            **params.model_dump(mode="json")}


def generate_bbo_target(config, metadata, bbo, allowed="BOTH"):
    """Copy L1 exactly; each L2 uses the configured quote amount, rounded down."""
    bid, ask, bq, aq = (D(bbo[k]) for k in ("bid", "ask", "bid_qty", "ask_qty"))
    if not all(x.is_finite() and x > 0 for x in (bid, ask, bq, aq)) or bid >= ask:
        raise ValueError("bookTicker 买卖价与数量必须有效且不交叉")
    tick, step = D(metadata["tick_size"]), D(metadata["qty_step"])
    if tick <= 0 or step <= 0:
        raise ValueError("市场价格或数量步长无效")
    orders, skipped = [], []
    for side, price, qty, sign, rounding in (
        ("BUY", bid, bq, -1, ROUND_FLOOR), ("SELL", ask, aq, 1, ROUND_CEILING),
    ):
        if allowed not in ("BOTH", side):
            continue
        # Never disguise a rounded/size-adjusted quote as an exact source copy.
        if price % tick or qty % step:
            return {"orders": [], "skipped_sides": [], "bbo": dict(bbo), "pause": True,
                    "reason": "上游一档不能原样下单，请检查本地价格/数量精度与最小下单限制"}
        if qty < D(metadata["min_qty"]) or price * qty < D(metadata["min_notional"]) or (metadata.get("max_qty") is not None and qty > D(metadata["max_qty"])):
            skipped.append(side + "1")
        else:
            orders.append({"side": side, "level": 1, "price": str(price), "qty": str(qty), "notional": str(price * qty)})
        second_price = (price * (1 + sign * D(config["second_level_distance_bps"]) / 10000) / tick).to_integral_value(rounding=rounding) * tick
        second_price = min(second_price, price - tick) if side == "BUY" else max(second_price, price + tick)
        if second_price <= 0:
            skipped.append(side + "2")
            continue
        second_qty = (D(config["second_level_notional"]) / second_price / step).to_integral_value(rounding=ROUND_FLOOR) * step
        if second_qty <= 0 or second_qty < D(metadata["min_qty"]) or second_price * second_qty < D(metadata["min_notional"]) or (metadata.get("max_qty") is not None and second_qty > D(metadata["max_qty"])):
            skipped.append(side + "2")
            continue
        orders.append({"side": side, "level": 2, "price": str(second_price), "qty": str(second_qty), "notional": str(second_price * second_qty)})
    return {"orders": orders, "skipped_sides": skipped, "bbo": dict(bbo)}


class SimpleBBOWorker(MakerExecutionWorker):
    """Reuse confirmed execution/recovery helpers, bypass the multi-level planner/executor."""
    strategy_key = "SIMPLE_BBO"
    def __init__(self, manager, record):
        # Base constructor initializes bounded execution tracking; no network/tasks start here.
        super().__init__(manager, record)
        if record["metadata"].get("product_type") == "SPOT":
            self.adapter = manager.spot_adapter
        self.record = record
        self.config = deepcopy(record["config"])
        self.feed = LadderFeed(self.config["primary_source"]["symbol"], require_quantities=True, product_type=record["metadata"].get("product_type", "PERP"))
        self._enabled_feed = False

    async def start(self):
        self.tasks = [asyncio.create_task(self._simple_loop(), name=f"simple-bbo-{self.symbol}")]

    async def set_record(self, record):
        if record == self.record:
            return
        source_changed = record["config"]["primary_source"] != self.config["primary_source"]
        if source_changed:
            await self.feed.stop()
            self.feed = LadderFeed(record["config"]["primary_source"]["symbol"], require_quantities=True, product_type=record["metadata"].get("product_type", "PERP"))
            self._enabled_feed = False
            self.reconfigure_clear = True
        if record["metadata"] != self.metadata and hasattr(self.adapter, "invalidate_metadata"):
            self.adapter.invalidate_metadata(self.symbol)
        self.record = record
        self.config = deepcopy(record["config"])
        self.metadata = record["metadata"]
        self.latest = None
        self._calculation_key = None

    def _rate_available(self):
        now = time.monotonic()
        while self._ops and self._ops[0] <= now - 1:
            self._ops.popleft()
        return len(self._ops) < 20

    def calculate(self, now):
        if self.unknown or self.recovering or self.reconfigure_clear:
            return self._paused_target("等待原请求终态及旧订单清查", "RECOVERING")
        if not self.config["enabled"]:
            return self._paused_target("人工停用")
        m = self.metadata
        if self.manager.control_error or not m["is_active"] or m["paper_status"] != "TRADING" or m["contract_trading_mode"] != "normal":
            return self._paused_target("配置同步异常或市场暂停")
        allowed = self.record.get("external_control", {}).get("allowed_quote_sides", "NONE")
        if allowed not in ("BOTH", "BUY", "SELL"):
            return self._paused_target("外部控制禁止报价")
        source = self.feed.snapshot(now)
        self.state = source["state"]
        self.last_source = source.get("observation")
        if not source["can_quote"]:
            return self._paused_target(source["reason"], source["state"])
        bbo = source["observation"]
        key = (self.record["desired_version"], m["metadata_version"], allowed,
               *(bbo[k] for k in ("bid", "ask", "bid_qty", "ask_qty")))
        if key == self._calculation_key and self.last_target is not None:
            if self.last_target.get("pause"):
                self.state = "PAUSED"
                self.pending_reason = self.last_target["reason"]
            return self.last_target
        target = generate_bbo_target(self.config, m, bbo, allowed)
        if target.get("pause"):
            self.state = "PAUSED"
            self.pending_reason = target["reason"]
        target.update(version=self.record["desired_version"], metadata_version=m["metadata_version"])
        self._calculation_key = key
        self.last_target = target
        self.active_version = target["version"]
        return target

    async def reconcile(self, target):
        if self.unknown or self.recovering or self.reconfigure_clear or target.get("pause"):
            return await super().reconcile(target)
        await self._refresh_orders()
        wanted = {(side_of(o), o["level"]): o for o in target["orders"]}
        # Recovery can leave a confirmed replacement beside its predecessor.
        # Drain that duplicate before starting another replacement (at most 5 orders).
        for side in ("bid", "ask"):
            orders = [o for o in self.orders if side_of(o) == side]
            for index, order in enumerate(sorted(orders, key=lambda o: D(o["price"]), reverse=side == "bid")):
                if order["order_id"] not in self.slots:
                    self._assign_slot(order, (side, index + 1))
        for slot in set(self.slots[o["order_id"]] for o in self.orders):
            orders = [o for o in self.orders if self.slots[o["order_id"]] == slot]
            if len(orders) > 1:
                goal = wanted.get(slot)
                keep = next((o for o in orders if goal and D(o["price"]) == D(goal["price"]) and leaves(o) == D(goal["qty"])), orders[-1])
                for order in orders:
                    if order is not keep:
                        if not self._rate_available():
                            return
                        await self._cancel(order)
        for order in list(self.orders):
            if self.slots.get(order["order_id"]) not in wanted:
                if not self._rate_available():
                    return
                await self._cancel(order)
        occupied = {self.slots.get(o["order_id"]) for o in self.orders}
        # A new feed tick may interrupt each write. Missing/oldest slots must get
        # their turn before refreshing the first bid again on a slower host.
        pending = dict(sorted(wanted.items(), key=lambda item: (
            item[0] in occupied, self.last_placed.get(item[0], 0))))
        while pending:
            progressed = False
            for slot, goal in list(pending.items()):
                current = self.calculate(time.monotonic())
                self.latest = current
                if current is not target or not self._target_current(target):
                    return
                old = [o for o in self.orders if self.slots.get(o["order_id"]) == slot]
                if any(D(o["price"]) == D(goal["price"]) and leaves(o) == D(goal["qty"]) for o in old):
                    del pending[slot]
                    progressed = True
                    continue
                # Move the opposite side out of the way first on a price jump.
                # Never cross our own still-resting quote during replacement.
                if any(side_of(o) != slot[0] and
                       (D(goal["price"]) >= D(o["price"]) if slot[0] == "bid" else D(goal["price"]) <= D(o["price"]))
                       for o in self.orders):
                    continue
                if not self._rate_available() or len(self._ops) + 1 + len(old) > 20:
                    return
                placed = await self._place(goal)
                if placed is None or leaves(placed) <= 0:
                    return  # Filled/rejected new quote is not replacement depth.
                for order in old:
                    await self._cancel(order)
                del pending[slot]
                progressed = True
            if not progressed:
                self.pending_reason = "等待旧对手报价移出，避免新旧报价交叉"
                return
        for order in list(self.orders):
            if self.slots.get(order["order_id"]) not in wanted:
                if not self._rate_available():
                    return
                await self._cancel(order)
        if len(self.orders) == len(wanted) and all(
            self.slots.get(o["order_id"]) in wanted and D(o["price"]) == D(wanted[self.slots[o["order_id"]]]["price"])
            and leaves(o) == D(wanted[self.slots[o["order_id"]]]["qty"]) for o in self.orders
        ):
            self.converged_version = target["version"]
            self.converged_metadata_version = target["metadata_version"]
            self.pending_reason = "档位不满足市场下单限制，跳过：" + ", ".join(target["skipped_sides"]) if target["skipped_sides"] else None
            self.last_error = None

    async def _simple_loop(self):
        try:
            while not self.stop_event.is_set():
                started = self.heartbeat = time.monotonic()
                try:
                    if self.config["enabled"] and not self._enabled_feed:
                        await self.feed.start()
                        self._enabled_feed = True
                    elif not self.config["enabled"] and self._enabled_feed:
                        await self.feed.stop()
                        self._enabled_feed = False
                    # A disabled, already-cleared instance does no adapter/DB polling.
                    if self.config["enabled"] or self.recovering or self.orders or self.unknown:
                        self.latest = self.calculate(started)
                        self._compute.append((time.monotonic() - started) * 1000)
                        await self.reconcile(self.latest)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)
                    if not getattr(exc, "definitive", False):
                        self.unknown = self.unknown or {"kind": "snapshot", "error": str(exc)}
                    self.state = "PAUSED"
                await asyncio.sleep(max(.01, (.1 if self.config["enabled"] else .5) - (time.monotonic() - started)))
        finally:
            await self.feed.stop()

    def status(self):
        return {"strategy_key": "SIMPLE_BBO", "enabled": self.config["enabled"], "state": self.state,
                "desired_version": self.record["desired_version"], "active_version": self.active_version,
                "converged_version": self.converged_version, "source": self.last_source,
                "open_order_count": len(self.orders), "own_orders": self.orders,
                "target": self.last_target, "pending_reason": self.pending_reason,
                "cancel_status": self.cancel_status, "unknown_count": int(bool(self.unknown)),
                "inflight_count": int(self.inflight is not None), "last_error": self.last_error,
                "metrics": {"cycle_ms": 100, "feed_updates": self.feed.updates,
                            "compute_ms": self._compute[-1] if self._compute else None}}
