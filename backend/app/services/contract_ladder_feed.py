"""Public BBO with quiet-market verification and same-source REST backup."""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from uuid import uuid4
from urllib.parse import quote

import httpx
import websockets


class LadderFeed:
    WS_BASE = "wss://fstream.binance.com/public/ws"
    REST_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

    def __init__(self, symbol: str, *, require_quantities=False, product_type="PERP"):
        if product_type == "SPOT":
            self.WS_BASE = "wss://stream.binance.com:9443/ws"
            self.REST_URL = "https://api.binance.com/api/v3/ticker/bookTicker"
        self.require_quantities = require_quantities
        self.symbol = symbol.upper()
        self.ws = None
        self.rest = None
        self.connected = False
        self.session = ""
        self.sequence = -1
        self.invalid_reason = None
        self._invalid_at = None
        self.last_error = None
        self.updates = 0
        self.rest_errors = 0
        self._tasks = []
        self._settings = {"quiet_verify_ms": 20000, "stop_ms": 5000,
                          "rest_interval_ms": 1000, "rest_timeout_ms": 1500}
        self._verified = None
        self._rest_required = False
        self._last_rest_attempt = float("-inf")

    def configure(self, settings):
        if set(settings) != set(self._settings):
            raise ValueError("行情配置必须使用静默核验、宽限/有效期、REST周期和超时四个字段")
        self._settings = dict(settings)

    @staticmethod
    def age(observation, now=None):
        if observation is None:
            return float("inf")
        now = time.monotonic() if now is None else now
        local_age = max(0, (now - observation["received_monotonic"]) * 1000)
        return max(local_age, local_age + observation.get("event_age_at_receive_ms", 0))

    def ingest(self, data, *, transport, session=None, received=None, request_started=None):
        received = time.monotonic() if received is None else received
        if transport == "WS" and session != self.session:
            return False
        newer_ws = transport == "REST" and request_started is not None and self.ws is not None and (
            self.ws["received_monotonic"] > request_started and self.connected
            and self.age(self.ws, received) <= self._settings["stop_ms"]
        )
        if transport == "WS":
            try:
                if int(data.get("u", 0)) <= self.sequence:
                    return False
            except (ValueError, TypeError):
                return False
        try:
            if str(data.get("s", data.get("symbol", self.symbol))).upper() != self.symbol:
                raise ValueError("上游标的与绑定不符")
            bid = Decimal(str(data.get("b", data.get("bidPrice"))))
            ask = Decimal(str(data.get("a", data.get("askPrice"))))
            if not bid.is_finite() or not ask.is_finite() or not 0 < bid < ask:
                raise ValueError("上游BBO必须满足0<买价<卖价")
            quantities = {}
            if self.require_quantities:
                for key, ws_key, rest_key in (("bid_qty", "B", "bidQty"), ("ask_qty", "A", "askQty")):
                    qty = Decimal(str(data.get(ws_key, data.get(rest_key))))
                    if not qty.is_finite() or qty <= 0:
                        raise ValueError("上游bookTicker买卖数量必须为有限正数")
                    quantities[key] = str(qty)
            seq = int(data.get("u", data.get("lastUpdateId", 0)))
            event_ms = int(data.get("E", 0))
            event_age = max(0, time.time() * 1000 - event_ms) if event_ms and transport == "WS" else 0
            if transport == "WS":
                if seq <= self.sequence:
                    return False
                if event_ms and event_ms - time.time() * 1000 > 5000:
                    raise ValueError("上游事件时间超前，需检查校时")
                self.sequence = seq
                if event_age > self._settings["stop_ms"]:
                    raise ValueError("新到达的WS报价事件已过期，等待同源REST核验")
            elif request_started is None or (received - request_started) * 1000 > self._settings["rest_timeout_ms"]:
                return False
        except (ValueError, TypeError, InvalidOperation) as exc:
            if newer_ws:
                return False
            self.invalid_reason = str(exc)
            self._invalid_at = received
            # An invalid new observation fences off earlier WS data. A REST
            # request already in flight cannot prove post-error recovery.
            self.ws = None
            self._verified = None
            self._rest_required = True
            return False
        observation = {**quantities, "exchange": "BINANCE", "symbol": self.symbol, "transport": transport,
                       "bid": str(bid), "ask": str(ask), "source_session": session or self.session,
                       "source_sequence": seq, "received_monotonic": received,
                       "received_at": int(time.time() * 1000), "observed_at": event_ms or int(time.time() * 1000),
                       "event_age_at_receive_ms": event_age if transport == "WS" else (received-request_started)*1000,
                       "request_latency_ms": (received-request_started)*1000 if request_started is not None else None}
        if transport == "WS":
            self.ws = observation
            self.updates += 1
            self._verified = observation
            self._rest_required = False
        else:
            # Retain the independent response, but never let a pre-error
            # request clear a newer invalid-source fence or roll back new WS.
            if self.rest is not None and request_started < self.rest["request_started_monotonic"]:
                return False
            observation["request_started_monotonic"] = request_started
            self.rest = observation
            if self._invalid_at is not None and request_started < self._invalid_at:
                return True
            if not newer_ws:
                same = self._current_ws() and all(Decimal(self.ws[k]) == Decimal(observation[k]) for k in ("bid", "ask"))
                if same and not self._rest_required:
                    self._verified = observation
                else:
                    self._rest_required = True
        self.invalid_reason = None
        self._invalid_at = None
        return True

    def _current_ws(self):
        return bool(self.connected and self.ws and self.ws["source_session"] == self.session)

    def snapshot(self, now=None):
        now = time.monotonic() if now is None else now
        cfg = self._settings
        if self.invalid_reason:
            return {"state": "PAUSED", "reason": self.invalid_reason, "observation": None, "can_quote": False}
        if self._current_ws() and not self._rest_required:
            age = self.age(self._verified, now)
            if age <= cfg["quiet_verify_ms"] + cfg["stop_ms"]:
                return self._result(self._verified, now, "NORMAL",
                                    "行情安静，正在同源REST核验" if age >= cfg["quiet_verify_ms"] else None)
            return {"state": "PAUSED", "reason": "静默行情核验超时，等待有效报价", "observation": None, "can_quote": False}
        # Disconnection/missed changes use the latest genuinely observed BBO
        # for at most stop_ms; a fresh cached quote bridges the first request.
        candidates = [o for o in (self.rest, self.ws) if self.age(o, now) <= cfg["stop_ms"]]
        if candidates:
            observation = min(candidates, key=lambda o: self.age(o, now))
            return self._result(observation, now, "REST_BACKUP",
                                "等待同源REST，暂用最近有效报价" if observation["transport"] == "WS" else "使用同源REST备用报价")
        return {"state": "PAUSED", "reason": "无有效备用报价，等待同源REST", "observation": None, "can_quote": False}

    def _result(self, observation, now, state, reason=None):
        return {"state": state, "reason": reason, "can_quote": True,
                "observation": {**observation, "age_ms": round(self.age(observation, now), 2),
                                "event_age_ms": round(self.age(observation, now), 2),
                                "verification_age_ms": round(self.age(observation, now), 2),
                                "ws_message_age_ms": round(self.age(self.ws, now), 2) if self.ws else None,
                                "verification_status": "QUIET_VERIFIED" if state == "NORMAL" and observation["transport"] == "REST" else state},
                # Transport health is not a risk multiplier. Dynamic protection
                # and the platform's allowed-side control remain independent.
                "mode": {"name": state, "extra_bps": "0", "depth_ratio": "1"}}

    async def start(self):
        if not self._tasks:
            self._tasks = [asyncio.create_task(self._ws_loop(), name=f"ladder-feed-{self.symbol}"),
                           asyncio.create_task(self._rest_loop(), name=f"ladder-rest-{self.symbol}")]

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.connected = False

    async def _ws_loop(self):
        while True:
            self.session = uuid4().hex
            self.sequence = -1
            try:
                async with websockets.connect(f"{self.WS_BASE}/{quote(self.symbol.lower(), safe="")}@bookTicker", open_timeout=10,
                                              ping_interval=20, ping_timeout=10, close_timeout=2, max_queue=1) as ws:
                    self.connected = True
                    async for raw in ws:
                        self.ingest(json.loads(raw), transport="WS", session=self.session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"WS: {type(exc).__name__}"
            finally:
                self.connected = False
                self._rest_required = True
            await asyncio.sleep(1)

    async def _rest_loop(self):
        async with httpx.AsyncClient() as client:
            while True:
                started = time.monotonic()
                if self.rest_due(started):
                    self._last_rest_attempt = started
                    try:
                        response = await client.get(self.REST_URL, params={"symbol": self.symbol},
                                                    timeout=self._settings["rest_timeout_ms"]/1000)
                        response.raise_for_status()
                        self.ingest(response.json(), transport="REST", request_started=started)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.rest_errors += 1
                        self.last_error = f"REST: {type(exc).__name__}"
                await asyncio.sleep(.1)

    def rest_due(self, now=None):
        now = time.monotonic() if now is None else now
        cfg = self._settings
        needs_verification = (not self._current_ws() or self._rest_required or self.invalid_reason
                              or self.age(self._verified, now) >= cfg["quiet_verify_ms"])
        return bool(needs_verification and (now-self._last_rest_attempt)*1000 >= cfg["rest_interval_ms"])
