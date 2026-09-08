"""Per-market latest-target coordinator for the ordinary limit/cancel strategy."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import time
from uuid import uuid4

from sqlalchemy import select, update

from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from app.models.market_strategy_config import MarketStrategyConfig
from app.services.contract_ladder_feed import LadderFeed
from app.services.maker_plugins import INTERNAL_MAKER_STRATEGIES, internal_validate, internal_worker
from app.services.maker_lifecycle import maker_serialized
from app.services.maker_execution import MakerExecutionWorker

STRATEGY_KEY = "CONTRACT_LADDER"
CAPABILITIES = {"post_only_supported": False, "ioc_supported": False, "amend_supported": False,
                "order_recovery_mode": "SNAPSHOT", "batch_size": 30, "pending_plan_slots": 1,
                "inflight_regular_batches": 1, "external_control_mode": "MANUAL"}
D = lambda x: Decimal(str(x))


def market_metadata(market, maker_uid):
    def clean(value, places):
        return format(D(value).quantize(Decimal(1).scaleb(-places)), "f")
    result = {"symbol": market.symbol, "product_type": market.product_type, "market_id": market.id, "maker_uid": maker_uid,
              "tick_size": clean(market.price_tick, market.price_precision),
              "qty_step": clean(market.qty_step, market.qty_precision),
              "min_qty": clean(market.min_qty, market.qty_precision),
              "min_notional": "0", "max_qty": None,
              "contract_multiplier": "1", "price_source_symbol": market.price_source_symbol,
              "is_active": market.is_active, "paper_status": market.paper_status,
              "contract_trading_mode": market.contract_trading_mode}
    result["metadata_version"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:16]
    return result


def side_of(order):
    return "bid" if str(order["side"]).upper() in {"BUY", "BID"} else "ask"


def leaves(order):
    return D(order.get("remaining_quantity", order.get("leaves_qty", order.get("qty", "0"))))




class ContractLadderService:
    def __init__(self, runtime, contract_service, session_factory, adapter=None):
        from app.services.contract_ladder_adapter import ContractLadderAdapter
        self.runtime = runtime
        self.sessions = session_factory
        self.adapter = adapter or ContractLadderAdapter(runtime, contract_service, session_factory)
        from app.services.spot_bbo_adapter import SpotBBOAdapter
        self.spot_adapter = SpotBBOAdapter(runtime, getattr(runtime, "order_service", None), session_factory)
        self.workers = {}
        self.records = {}
        self.control_error = None
        self._publish_lock = asyncio.Lock()
        self._refresh_lock = asyncio.Lock()

    async def refresh(self):
        async with self._refresh_lock:
            await self._refresh()

    async def _refresh(self):
        async with self.sessions() as session:
            rows = (await session.execute(select(MarketStrategyConfig, Market, MarketBotAccount)
                    .join(Market, Market.id==MarketStrategyConfig.market_id)
                    .join(MarketBotAccount, MarketBotAccount.market_id==Market.id)
                    .where(MarketStrategyConfig.strategy_key.in_(INTERNAL_MAKER_STRATEGIES),
                           MarketBotAccount.strategy_role==STRATEGY_KEY,
                           MarketBotAccount.is_enabled.is_(True)))).all()
            records = {}
            for row, market, binding in rows:
                # Retain deselected LADDER for its existing editor, but never let a
                # historical algorithm overwrite the selected worker for this market.
                if market.symbol in records and not row.is_enabled:
                    continue
                doc = deepcopy(row.config_json)
                if not isinstance(doc.get("config"), dict):
                    continue
                metadata = market_metadata(market, binding.user_id)
                normalized = internal_validate(row.strategy_key, doc["config"], metadata, CAPABILITIES)
                record = {"symbol":market.symbol,"display_symbol":market.symbol.removesuffix("-PERP").lower(),
                          "strategy_key": row.strategy_key,
                          "market_id":market.id,"maker_uid":binding.user_id,"row_id":row.id,
                          "metadata":metadata, "config":normalized,
                          "desired_version":doc["desired_version"], "capabilities":CAPABILITIES,
                          "external_control":doc.get("external_control", {"mode":"MANUAL", "control_version":0,
                                                     "allowed_quote_sides":"BOTH","reason":"人工报价控制"})}
                if not row.is_enabled:
                    record["config"]["enabled"] = False
                if not binding.is_enabled:
                    record["external_control"] = {"mode":"MANUAL", "control_version":-1,
                                                 "allowed_quote_sides":"NONE","reason":"平台禁用机器人绑定"}
                records[market.symbol] = record
        self.control_error = None
        self.records = records
        for symbol in list(self.workers):
            if symbol not in records:
                await self.workers.pop(symbol).stop()
        for symbol, record in records.items():
            old_worker = self.workers.get(symbol)
            if old_worker and (old_worker.uid != record['maker_uid'] or getattr(old_worker, 'strategy_key', STRATEGY_KEY) != record['strategy_key']):
                if old_worker.orders or old_worker.config.get('enabled') or getattr(old_worker, 'inflight', None) or getattr(old_worker, 'unknown', None):
                    raise ValueError('更换执行 UID 前必须停止并撤净旧实例订单')
                await old_worker.stop()
                del self.workers[symbol]
            if symbol not in self.workers:
                self.workers[symbol] = internal_worker(record['strategy_key'])(self, record)
                await self.workers[symbol].start()
            else:
                await self.workers[symbol].set_record(record)

    async def run(self):
        try:
            while True:
                try:
                    await self.refresh()
                    for worker in self.workers.values():
                        if any(t.done() for t in worker.tasks) or time.monotonic()-worker.heartbeat>3:
                            worker.config["enabled"] = False
                            worker.last_error = "独立管理检查发现worker失联，停止新增并撤单"
                            await worker.stop()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.control_error = str(exc)
                await asyncio.sleep(1)
        finally:
            await asyncio.gather(*(w.stop() for w in self.workers.values()), return_exceptions=True)

    def read(self, symbol):
        result = deepcopy(self.records[symbol])
        result["runtime"] = self.workers[symbol].status()
        result["status"] = result["runtime"]
        return result

    def preview(self, symbol, draft, test_amounts=None, bbo=None):
        worker = self.workers[symbol]
        from app.services.maker_plugins import get_plugin
        plugin = get_plugin(self.records[symbol].get("strategy_key", STRATEGY_KEY))
        return plugin.call("preview", worker, draft, test_amounts=test_amounts, bbo=bbo)

    @maker_serialized
    async def publish(self, symbol, draft, expected_version, user_id):
        async with self._publish_lock:
            record = self.records[symbol]
            normalized = internal_validate(record.get("strategy_key", STRATEGY_KEY), draft, record["metadata"], CAPABILITIES)
            if str(normalized["primary_source"]["exchange"]).upper() != "BINANCE":
                raise ValueError("当前只核验了Binance合约bookTicker接入")
            if normalized["primary_source"]["symbol"] != record["metadata"]["price_source_symbol"]:
                raise ValueError("上游标的必须与本所市场映射一致；更改映射需平台协调")
            if normalized["enabled"]:
                preview = self.preview(symbol, normalized)
                if not preview.get("preflight", {}).get("ok", False):
                    raise ValueError("预检未通过，请先修正所有运行模式的金额或精度问题")
            async with self.sessions() as session:
                from app.services.strategy_accounts import validate_uid
                await validate_uid(session, record['maker_uid'], record['market_id'], 'maker')
                row = await session.get(MarketStrategyConfig,record["row_id"])
                if normalized["enabled"] and not row.is_enabled:
                    raise ValueError("该币对未选择 LADDER，请先在铺单策略页面切换")
                if row.config_json["desired_version"] != expected_version:
                    raise VersionConflict("配置已由其他操作更新，请重新读取")
                document = {**row.config_json, "config":normalized,"desired_version":expected_version+1}
                result = await session.execute(update(MarketStrategyConfig).where(
                    MarketStrategyConfig.id==row.id,
                    MarketStrategyConfig.config_json["desired_version"].as_integer()==expected_version)
                    .values(config_json=document,updated_by_user_id=user_id))
                if result.rowcount != 1:
                    raise VersionConflict("版本冲突，未覆盖较新的配置")
                await session.commit()
            await self.refresh()
            return self.read(symbol)

    @maker_serialized
    async def control(self, symbol, allowed, reason, expected_control_version):
        async with self._publish_lock:
            async with self.sessions() as session:
                row = await session.get(MarketStrategyConfig,self.records[symbol]["row_id"])
                previous = row.config_json.get("external_control", {})
                if int(previous.get("control_version",0)) != expected_control_version:
                    raise VersionConflict("报价控制版本冲突，旧指令不能覆盖当前状态")
                doc=deepcopy(row.config_json)
                doc["external_control"]={"mode":"MANUAL","control_version":expected_control_version+1,
                                         "allowed_quote_sides":allowed,"reason":reason}
                row.config_json=doc
                await session.commit()
            await self.refresh()
            return self.read(symbol)


class VersionConflict(ValueError):
    pass


def __getattr__(name):
    if name == "LadderWorker":
        from app.maker_strategies.contract_ladder.worker import LadderWorker
        return LadderWorker
    raise AttributeError(name)
