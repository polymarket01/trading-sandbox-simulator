"""FLOW control plane. The independent process schedules; this adapter owns admission.

Virtual prints affect only ephemeral public market data. Real IOC uses a bound
flow UID and the normal order service, with no internal-maker privileges.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
import json
import logging
import os
import random
from pathlib import Path
import secrets
import sys
import time

from pydantic import BaseModel, Field, model_validator
from typing import Literal
from sqlalchemy import select

from app.models.market import Market
from app.models.market_bot_account import MarketBotAccount
from app.models.user import User
from app.schemas.api import OrderCreateRequest, ContractOrderCreateRequest
from app.services.synthetic_flow_service import broadcast_synthetic_flow
from app.services.flow_activity import FlowActivity

D = Decimal


class FlowConfig(BaseModel):
    mode: Literal['virtual_volume', 'real_ioc_sandbox'] = 'virtual_volume'
    enabled: bool = False
    virtual_allow_touch: bool = False
    uid: int | None = Field(default=None, gt=0)
    interval_min_seconds: float = Field(default=1.5, ge=.02, le=114)
    interval_max_seconds: float = Field(default=2.5, ge=.02, le=114)
    min_quote: D = Field(default=D('100'), gt=0, le=10000000)
    max_quote: D = Field(default=D('1000'), gt=0, le=10000000)
    turnover_quote_per_min: D = Field(default=D('30000'), gt=0, le=100000000)
    volatility_enabled: bool = True
    real_ioc_volatility_enabled: bool = False
    volatility_window_seconds: int = Field(default=10, ge=2, le=60)
    volatility_return_bps: float = Field(default=5, gt=0, le=10000)
    volatility_range_bps: float = Field(default=8, gt=0, le=10000)
    volatility_max_multiplier: float = Field(default=100, ge=1, le=100)
    max_level_take_ratio: D = Field(default=D('.25'), gt=0, le=1)

    @model_validator(mode='before')
    @classmethod
    def migrate_interval(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if 'interval_min_seconds' not in value and 'interval_max_seconds' not in value and ('interval_ms' in value or 'interval_jitter' in value):
                base = float(value.get('interval_ms', 2000)) / 1000
                jitter = float(value.get('interval_jitter', .25))
                value['interval_min_seconds'] = round(base * (1-jitter), 9)
                value['interval_max_seconds'] = round(base * (1+jitter), 9)
            value.pop('interval_ms', None)
            value.pop('interval_jitter', None)
        return value

    @model_validator(mode='after')
    def validate_bounds(self):
        if self.interval_max_seconds < self.interval_min_seconds:
            raise ValueError('最长间隔不能小于最短间隔')
        if self.max_quote < self.min_quote:
            raise ValueError('最大单笔金额不能小于最小金额')
        return self


def inside_spread_price(bid: D, ask: D, tick: D, fair: D | None = None) -> D | None:
    if tick <= 0 or bid <= 0 or ask - bid <= tick:
        return None
    lower = (bid / tick).to_integral_value(rounding=ROUND_DOWN) * tick + tick
    upper = ((ask / tick).to_integral_value(rounding="ROUND_CEILING") - 1) * tick
    if lower > upper:
        return None
    target = fair if fair is not None and fair.is_finite() and fair > 0 else (bid + ask) / 2
    price = (target / tick).to_integral_value(rounding=ROUND_DOWN) * tick
    return min(upper, max(lower, price))


class FlowAccessFilter(logging.Filter):
    def filter(self, record):
        # High-frequency internal polling must not grow the runner access log.
        return "/internal/flow/" not in record.getMessage()


class IndependentFlowService:
    def __init__(self, app, sessions, directory: Path):
        self.app, self.sessions = app, sessions
        logging.getLogger("uvicorn.access").addFilter(FlowAccessFilter())
        self.path = directory / 'flow_config.json'
        self.token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()
        self.configs = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.configs = {symbol: {**doc, 'config': FlowConfig.model_validate(doc['config']).model_dump(mode='json')} for symbol, doc in self.configs.items()}
        self.activity = {}
        self.activity_sample_at = -1e20
        self.metrics = {}
        self.seen = OrderedDict()
        self.last_tick = {}
        self.budget = {}
        self.process = None
        self.heartbeat = 0.0
        self.error = None
        self.blocked = {}

    def sample_activity(self):
        now = time.monotonic()
        if now - self.activity_sample_at < 1:
            return
        self.activity_sample_at = now
        runtime = getattr(self.app.state, 'runtime', None)
        ladder = getattr(self.app.state, 'contract_ladder_service', None)
        for symbol, doc in self.configs.items():
            config = FlowConfig.model_validate(doc['config'])
            if not config.enabled:
                self.activity.pop(symbol, None)
                continue
            price, source = None, 'local_bbo'
            worker = getattr(ladder, 'workers', {}).get(symbol)
            if worker is not None:
                # Reuse the existing feed, not the virtual trade tape (no feedback loop).
                observation = getattr(worker, 'last_source', None) or {}
                source = 'binance:' + str(observation.get('symbol', '')) + ':' + str(observation.get('source_session', ''))
                if observation and worker.feed.age(observation, now) <= 1500:
                    bid, ask = float(observation.get('bid', 0)), float(observation.get('ask', 0))
                    if 0 < bid <= ask:
                        price = (bid + ask) / 2
            else:
                book = getattr(runtime, 'published_orderbooks', {}).get(symbol)
                if book and book.bids and book.asks and time.time()*1000 - book.updated_at_ms <= 5000:
                    bid, ask = float(book.bids[0][0]), float(book.asks[0][0])
                    if 0 < bid <= ask:
                        price = (bid + ask) / 2
            self.activity.setdefault(symbol, FlowActivity()).update(now, price, source, config)

    def worker_config(self):
        self.sample_activity()
        return {'items': self.configs}

    def listing(self):
        self.sample_activity()
        return {'activity_version': 1, 'items': self.configs, 'metrics': self.metrics, 'activity': {symbol: signal.result for symbol, signal in self.activity.items()},
                'process': {'pid': self.process.pid if self.process else None,
                            'running': self.process is not None and self.process.returncode is None,
                            'heartbeat_age_seconds': round(time.monotonic()-self.heartbeat, 1) if self.heartbeat else None,
                            'error': self.error}}

    async def save(self, symbol, config, expected_version):
        config = FlowConfig.model_validate(config)
        async with self.lock:
            old = self.configs.get(symbol, {'version': 0})
            if old['version'] != expected_version:
                raise ValueError('配置版本已变化，请刷新后重试')
            async with self.sessions() as session:
                market = await session.scalar(select(Market).where(Market.symbol == symbol))
                if market is None:
                    raise ValueError('币对不存在')
                if config.mode == 'real_ioc_sandbox':
                    from app.services.strategy_accounts import allocate_accounts
                    assigned = await allocate_accounts(session, market, 'flow', config.mode, [config.uid])
                    config.uid = assigned[0]
                    await session.commit()
                else:
                    config.uid = None
            updated = {**self.configs, symbol: {'version': expected_version+1, 'config': config.model_dump(mode='json')}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(updated, ensure_ascii=False, indent=2))
            os.replace(temporary, self.path)
            self.configs = updated
            self.blocked.pop(symbol, None)
            self.activity.pop(symbol, None)
            self.activity_sample_at = -1e20
            return updated[symbol]

    async def flow_user(self, session, market, uid):
        from app.services.strategy_accounts import validate_uid
        await validate_uid(session, uid, market.id, 'flow')
        user = await session.scalar(select(User).join(MarketBotAccount, MarketBotAccount.user_id == User.id).where(
            User.id == uid, User.is_active.is_(True), User.role == 'mm_bot',
            MarketBotAccount.market_id == market.id, MarketBotAccount.role == 'flow',
            MarketBotAccount.is_enabled.is_(True),
        ))
        if user is None:
            raise ValueError('UID 必须是该币对启用的独立 FLOW 账户，不能复用内部 Maker 身份')
        return user

    async def tick(self, symbol, version, event_id, side, quote):
        # One admitted request at a time; config changes and pause wait for its
        # settlement, so no old-mode request can run after pause returns.
        async with self.lock:
            key = (symbol, event_id)
            if key in self.seen:
                return self.seen[key]
            doc = self.configs.get(symbol)
            if not doc or doc['version'] != version or not doc['config']['enabled']:
                return {'status': 'paused_or_reconfigured'}
            if symbol in self.blocked:
                return {"status": "blocked", "reason": self.blocked[symbol]}
            config = FlowConfig.model_validate(doc['config'])
            self.sample_activity()
            quote = D(str(quote))
            if side not in ('buy', 'sell') or not quote.is_finite() or not config.min_quote <= quote <= config.max_quote:
                raise ValueError('FLOW 请求超出配置范围')
            base_quote = quote
            signal = self.activity.get(symbol)
            allowed = config.volatility_enabled and (config.mode == 'virtual_volume' or config.real_ioc_volatility_enabled)
            multiplier = D(str(signal.result['multiplier'])) if allowed and signal else D(1)
            multiplier = min(D(str(config.volatility_max_multiplier)), max(D(1), multiplier))
            direction = signal.result.get('direction') if signal else None
            burst_direction = signal.result.get('burst_direction') if signal else None
            if multiplier > 1 and burst_direction in ('buy', 'sell'):
                side = burst_direction
                direction_reason = 'burst_follow'
            elif direction in ('buy', 'sell'):
                side = direction if random.random() < .8 else ('sell' if direction == 'buy' else 'buy')
                direction_reason = 'trend_80'
            else:
                direction_reason = 'random_50'
            quote *= multiplier
            now = time.monotonic()
            if now - self.last_tick.get(symbol, 0) < config.interval_min_seconds * .9:
                return {'status': 'waiting_interval'}
            self.last_tick[symbol] = now
            window, spent = self.budget.get(symbol, (now, D(0)))
            if now-window >= 60:
                window, spent = now, D(0)
            # Virtual volume has a bounded expanded ceiling: base budget × max multiplier.
            # IOC always retains the configured absolute financial turnover cap.
            charge = base_quote if config.mode == 'virtual_volume' else quote
            if spent + charge > config.turnover_quote_per_min:
                return {'status': 'turnover_limit'}
            self.budget[symbol] = window, spent+charge
            # Record before awaiting: a timeout/unknown result is never retried
            # with a different identity by the worker.
            self.seen[key] = {'status': 'pending_or_unknown'}
            while len(self.seen) > 4096:
                self.seen.popitem(last=False)
            try:
                result = await self._execute(symbol, config, event_id, side, quote)
            except Exception as exc:
                self.blocked[symbol] = str(exc)
                result = {'status': 'blocked', 'reason': str(exc)}
            result = {**result, 'base_quote': str(base_quote), 'effective_quote': str(quote), 'volume_multiplier': str(multiplier), 'side': side, 'direction_reason': direction_reason}
            self.seen[key] = result
            self.metrics[symbol] = {**result, 'at': datetime.now(UTC).isoformat(),
                                    'mode': config.mode, 'attempts': self.metrics.get(symbol, {}).get('attempts', 0)+1}
            return result

    def virtual_fair_price(self, symbol, bid, ask):
        # Reuse the maker's current public observation; never use virtual prints
        # as a reference and never add per-trade upstream network requests.
        ladder = getattr(self.app.state, 'contract_ladder_service', None)
        worker = getattr(ladder, 'workers', {}).get(symbol)
        observation = getattr(worker, 'last_source', None) or {}
        if observation:
            try:
                age = worker.feed.age(observation, time.monotonic())
                upstream_bid, upstream_ask = D(str(observation['bid'])), D(str(observation['ask']))
                if 0 <= age <= 1500 and upstream_bid.is_finite() and upstream_ask.is_finite() and 0 < upstream_bid <= upstream_ask:
                    return (upstream_bid + upstream_ask) / 2, 'upstream_book_ticker'
            except (KeyError, TypeError, ValueError, ArithmeticError):
                pass
        return (bid + ask) / 2, 'local_bbo_fallback'

    async def _execute(self, symbol, config, event_id, side, quote):
        runtime = self.app.state.runtime
        async with self.sessions() as session:
            market = await session.scalar(select(Market).where(Market.symbol == symbol))
            if market is None or not market.is_active or (market.product_type == 'PERP' and market.contract_trading_mode != 'normal'):
                return {'status': 'market_paused'}
            book, _, _ = await runtime.orderbook_snapshot(symbol, 1)
            if not book['bids'] or not book['asks']:
                return {'status': 'empty_book'}
            bid, ask = D(book['bids'][0][0]), D(book['asks'][0][0])
            tick = D(str(market.price_tick)).quantize(D(1).scaleb(-market.price_precision))
            # An interior print price is required only for virtual volume.
            # Real IOC executes at the opposite BBO even with a one-tick spread.
            inside = None
            if config.mode == 'virtual_volume':
                fair, price_source = self.virtual_fair_price(symbol, bid, ask)
                inside = inside_spread_price(bid, ask, tick, fair)
                if inside is None and config.virtual_allow_touch and tick > 0 and 0 < bid < ask and ask-bid <= tick and bid % tick == 0 and ask % tick == 0:
                    inside = ask if side == 'buy' else bid
                    price_source = 'best_quote_virtual_only'
                if inside is None:
                    return {'status': 'spread_le_one_tick'}
            step = D(str(market.qty_step)).quantize(D(1).scaleb(-market.qty_precision))
            price = inside if config.mode == 'virtual_volume' else ask if side == 'buy' else bid
            qty = (quote / price / step).to_integral_value(rounding=ROUND_DOWN)*step
            if config.mode == 'virtual_volume':
                if qty <= 0:
                    return {'status': 'below_quantity_step'}
                # No await between reading committed BBO and appending the print.
                item = runtime.market_data.ingest_display_trade(symbol, price=price, quantity=qty, side=side,
                    ts=datetime.now(UTC), trade_id='virtual-'+event_id, price_scale=market.price_precision,
                    qty_scale=market.qty_precision, source='virtual_volume')
                await broadcast_synthetic_flow(runtime, market, [item])
                return {'status': 'virtual_print', 'price': str(price), 'quantity': str(qty), 'financial_effect': False, 'fair_price': str(fair), 'price_source': price_source}
            user = await self.flow_user(session, market, config.uid)
            top_qty = D(book['asks' if side == 'buy' else 'bids'][0][1])
            qty = min(qty, (top_qty*config.max_level_take_ratio/step).to_integral_value(rounding=ROUND_DOWN)*step)
            if qty < D(market.min_qty) or qty*price < D(market.min_notional):
                return {'status': 'below_minimum_or_thin_book'}
            fields = dict(symbol=symbol, side=side, type='limit', tif='ioc', price=price, quantity=qty,
                          client_order_id='turnover-'+event_id)
            if market.product_type == 'PERP':
                service = self.app.state.contract_service
                position = await service.get_position(session, user.id, market, create=False)
                close = position is not None and position.is_active and position.side == ('short' if side == 'buy' else 'long')
                if close:
                    fields['quantity'] = min(qty, service._position_qty_without_storage_dust(market, position.quantity))
                payload = ContractOrderCreateRequest(**fields, position_action='close' if close else 'open', reduce_only=close)
                result = await service.place_order(session, user, payload)
            else:
                result = await self.app.state.order_service.place_order(session, user, OrderCreateRequest(**fields))
            return {'status': result['order']['status'], 'order_id': result['order']['order_id'], 'financial_effect': True}

    async def run(self, api_url):
        root = Path(__file__).resolve().parents[3]
        env = {**os.environ, 'FLOW_INTERNAL_TOKEN': self.token, 'FLOW_API_URL': api_url, 'FLOW_PARENT_PID': str(os.getpid())}
        try:
            while True:
                self.process = await asyncio.create_subprocess_exec(sys.executable, str(root/'flow_service.py'), env=env)
                await self.process.wait()
                self.error = f'FLOW 子进程退出 {self.process.returncode}，5 秒后重启'
                await asyncio.sleep(5)
        finally:
            if self.process is not None and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
