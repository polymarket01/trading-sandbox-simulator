"""Shared owned-order execution and recovery. No quote-planning algorithms."""
import asyncio
import time
from collections import deque
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4

D = lambda x: Decimal(str(x))

def side_of(order):
    return "bid" if str(order["side"]).upper() in {"BUY", "BID"} else "ask"

def leaves(order):
    return D(order.get("remaining_quantity", order.get("leaves_qty", order.get("qty", "0"))))

class MakerExecutionWorker:
    def __init__(self, manager, record):
        self.manager = manager
        self.adapter = manager.adapter
        self.symbol = record["symbol"]
        self.uid = record["maker_uid"]
        self.record = record
        self.config = deepcopy(record["config"])
        self.metadata = record["metadata"]
        self.latest = None
        self.target_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.tasks = []
        self.recovering = True
        self.reconfigure_clear = False
        self.active_version = 0
        self.converged_version = 0
        self.converged_metadata_version = None
        self.state = "RECOVERING"
        self.cancel_status = "PENDING"
        self.pending_reason = "启动清查全部自有订单"
        self.orders = []
        self.slots = {}
        self.last_placed = {}
        self._ops = deque(maxlen=4001)
        self._latencies = deque(maxlen=512)
        self._compute = deque(maxlen=512)
        self._counter = 0
        self._run = uuid4().hex[:12]
        self.inflight = None
        self.inflight_at = 0
        self.unknown = None
        self.last_error = None
        self.last_failure = None
        self.rejections = 0
        self.cancel_failures = 0
        self.heartbeat = time.monotonic()
        self.deep_checked = 0
        self.deep_cursor = 0
        self.last_source = None
        self.source_warning = None
        self.source_details = {}
        self.last_target = None
        self._reducing = False
        self.previous_budgets = None
        self._by_price = {}
        self._by_slot = {}
        self._amounts = {"bid": D(0), "ask": D(0)}
        self._orders_by_id = {}
        self._calculation_key = None
        self.price_tolerance_retained = 0
        self._prepared_target = None
        self._prepared_data = None
        self._head_signature = {side: None for side in ("bid", "ask")}
        self._head_changed_at = {side: time.monotonic() for side in ("bid", "ask")}
        self._head_attempt_at = {side: float("-inf") for side in ("bid", "ask")}
        self._head_cursor = {side: 0 for side in ("bid", "ask")}
        self._head_refresh_sequence = 0
        self.head_refresh_count = 0


    def _paused_target(self, reason, state="PAUSED"):
        self._calculation_key = None
        self.state = state
        self.pending_reason = reason
        return {"orders": [], "pause": True, "reason": reason, "version": self.record["desired_version"],
                "metadata_version": self.metadata["metadata_version"]}

    async def _refresh_orders(self):
        self.orders = await self.adapter.own_orders(self.symbol, self.uid)
        live_ids = {o["order_id"] for o in self.orders}
        self.slots = {oid: slot for oid, slot in self.slots.items() if oid in live_ids}
        self._by_price = {}
        self._by_slot = {}
        self._amounts = {"bid": D(0), "ask": D(0)}
        self._orders_by_id = {}
        for order in self.orders:
            oid, side, price, qty = order["order_id"], side_of(order), D(order["price"]), leaves(order)
            self._orders_by_id[oid] = order
            self._by_price.setdefault((side, price), []).append(order)
            self._amounts[side] += price * qty
            if oid in self.slots:
                self._by_slot.setdefault(self.slots[oid], []).append(order)
        self.on_orders_refreshed()
        # Successful authoritative I/O is progress even during a long reconciliation.
        self.heartbeat = time.monotonic()
        return self.orders

    def _assign_slot(self, order, slot):
        oid = order["order_id"]
        old = self.slots.get(oid)
        if old == slot:
            return
        if old in self._by_slot:
            self._by_slot[old] = [o for o in self._by_slot[old] if o["order_id"] != oid]
        self.slots[oid] = slot
        self._by_slot.setdefault(slot, []).append(order)

    async def _perform(self, coroutine, *, kind, request=None):
        now = time.monotonic()
        self._ops.append(now)
        self.inflight_at = now
        task = asyncio.create_task(coroutine)
        self.inflight = task
        try:
            # Shield leaves the original operation running; timeout is never a zero-execution claim.
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=1)
            except asyncio.TimeoutError:
                self.unknown = {"kind": kind, "started": now, **(request or {})}
                self.cancel_status = "UNKNOWN"
                result = await asyncio.shield(task)
            self._latencies.append((time.monotonic()-now)*1000)
            self.unknown = None
            self.heartbeat = time.monotonic()
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            if kind == "cancel":
                self.cancel_failures += 1
            else:
                self.rejections += 1
            # An adapter explicitly labels authoritative pre-execution rejection.
            if getattr(exc, "definitive", False):
                self.unknown = None
            else:
                self.unknown = {"kind": kind, "started": now, "error": str(exc), **(request or {})}
            raise
        finally:
            if task.done():
                self.inflight = None

    def _target_current(self, target):
        return (isinstance(target, dict) and target is self.latest and target.get("version")==self.record["desired_version"]
                and target.get("metadata_version")==self.metadata["metadata_version"]
                and self.config["enabled"] and not self.reconfigure_clear and not self.recovering
                and not self.unknown and not self.manager.control_error and not target.get("pause")
                and self.record.get("external_control", {}).get("allowed_quote_sides", "NONE")!="NONE")

    async def _cancel(self, order):
        await self._perform(self.adapter.cancel(self.symbol, self.uid, order["order_id"]), kind="cancel", request={"order_id": order["order_id"]})
        await self._refresh_orders()
        if any(o["order_id"] == order["order_id"] for o in self.orders):
            self.unknown = {"kind": "cancel", "order_id": order["order_id"]}
            raise RuntimeError("撤单返回后权威自有订单仍在簿")

    async def _place(self, target_order):
        slot = (side_of(target_order), int(target_order["level"]))
        self._counter += 1
        cid = f"cl-{self._run}-{slot[0][0]}{slot[1]}-{self._counter}"
        self.last_placed[slot] = time.monotonic()
        place = self.adapter.place
        admission = {}
        checked = getattr(self.adapter, "place_checked", None)
        if checked is not None:
            target = self.latest
            place = checked
            admission["admission_check"] = lambda: self._target_current(target)
        result = await self._perform(place(
            self.symbol, self.uid, client_order_id=cid, side=target_order["side"].lower(),
            price=target_order["price"], quantity=target_order["qty"], **admission), kind="place", request={"client_order_id": cid})
        order = result["order"]
        self.slots[order["order_id"]] = slot
        await self._refresh_orders()
        return next((o for o in self.orders if o["order_id"]==order["order_id"]), None)

    async def stop(self):
        self.config["enabled"] = False
        self.latest = self._paused_target("服务停止，撤销自有订单")
        deadline = time.monotonic()+10
        while time.monotonic()<deadline:
            self.target_event.set()
            if not self.orders and not self.inflight:
                break
            await asyncio.sleep(.1)
        self.stop_event.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.feed.stop()
        await self.stop_extra_feeds()

    async def reconcile(self, target):
        await self._refresh_orders()
        if self.unknown:
            identity = {k:self.unknown[k] for k in ("order_id","client_order_id") if k in self.unknown}
            if identity and self.inflight is None:
                queried = await self.adapter.query(self.symbol, self.uid, **identity)
                if not queried.get("financial_unknown") and (queried.get("order") or queried.get("status")=="NOT_FOUND_FINAL"):
                    self.unknown = None
                    self.recovering = True
                    self.pending_reason = "原请求已查清，撤旧后按最新目标恢复"
                    return
            self.pending_reason = "未知结果需原请求查询或停写清查，未重新提交"
            return
        ops = 0
        if target.get("pause") or self.recovering or self.reconfigure_clear:
            self.cancel_status = "PENDING" if self.orders else "CONFIRMED"
            for order in list(self.orders):
                if self.latest is not None and not self.latest.get("pause") and not (self.recovering or self.reconfigure_clear):
                    self.pending_reason = "有效行情已恢复，停止过时的整侧撤单计划"
                    return
                if ops >= 30 or not self._rate_available():
                    return
                await self._cancel(order)
                ops += 1
            if not self.orders:
                self.cancel_status = "CONFIRMED"
                self.recovering = False
                self.reconfigure_clear = False
                if target.get("version"):
                    self.active_version = target["version"]
                    self.converged_version = target["version"]
                    self.converged_metadata_version = target["metadata_version"]
            return

    def on_orders_refreshed(self):
        pass

    async def stop_extra_feeds(self):
        pass
