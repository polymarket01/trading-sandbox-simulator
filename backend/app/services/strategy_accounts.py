"""Account allocation belongs to strategy instances, not algorithm implementations."""
from decimal import Decimal
from sqlalchemy import select, or_
from fastapi import HTTPException
from app.models.user import User
from app.models.market_bot_account import MarketBotAccount
from app.models.order import Order
from app.models.contract_position import ContractPosition

from app.services.maker_plugins import INTERNAL_MAKER_STRATEGIES, installed_plugins

# Account capabilities are plugin metadata. UID ownership stays on market bindings.
ACCOUNT_SLOTS = {**{key: p.manifest['account_slots'] for key, p in installed_plugins().items()}, 'real_ioc_sandbox': 1, 'virtual_volume': 0}


async def validate_uid(session, uid, market_id, role, *, exclude_id=None):
    user = await session.get(User, uid)
    if user is None or user.role != 'mm_bot' or not user.is_active:
        raise HTTPException(422, 'UID 必须是已启用的机器人账户')
    bindings = (await session.scalars(select(MarketBotAccount).where(MarketBotAccount.user_id == uid))).all()
    for b in bindings:
        if b.id == exclude_id:
            continue
        if b.market_id != market_id or b.role != role:
            raise HTTPException(409, f'UID {uid} 已归属其他币对或角色；每个币对独占 UID，真实 FLOW 与 Maker 分开')
    return user


async def has_exposure(session, uid, market_id=None):
    order = select(Order.id).where(Order.user_id == uid, Order.status.in_(['new', 'partially_filled']))
    position = select(ContractPosition.id).where(ContractPosition.user_id == uid, *ContractPosition.active_filters())
    if market_id is not None:
        order = order.where(Order.market_id == market_id)
        position = position.where(ContractPosition.market_id == market_id)
    return await session.scalar(order.limit(1)) is not None or await session.scalar(position.limit(1)) is not None


async def allocate_accounts(session, market, role, strategy, requested=None):
    """Call inside the caller's transaction; repeats reuse bindings without funding resets."""
    from app.api.admin import create_market_bot_account
    from app.schemas.api import MarketBotCreateRequest
    slots = ACCOUNT_SLOTS[strategy]
    if any(u is not None and (not isinstance(u, int) or isinstance(u, bool) or u <= 0) for u in (requested or [])):
        raise HTTPException(422, 'UID 必须是正整数')
    existing = list((await session.scalars(select(MarketBotAccount).where(
        MarketBotAccount.market_id == market.id, MarketBotAccount.role == role,
        or_(MarketBotAccount.strategy_role != 'paper_default', MarketBotAccount.strategy_role.is_(None))).order_by(MarketBotAccount.is_enabled.desc(), MarketBotAccount.id))).all())
    if requested is not None and len(requested) > slots:
        raise HTTPException(422, f'此策略最多配置 {slots} 个执行 UID')
    requested = list(requested or []) + [None] * (slots - len(requested or []))
    if len({u for u in requested if u is not None}) != len([u for u in requested if u is not None]):
        raise HTTPException(422, '执行 UID 不能重复')
    chosen = []
    for uid in requested:
        if uid is None:
            candidate = next((b for b in existing if b.user_id not in [x.user_id for x in chosen]
                              and b.user_id not in requested), None)
            uid = candidate.user_id if candidate else None
        if uid is not None:
            await validate_uid(session, uid, market.id, role)
        binding = next((b for b in existing if b.user_id == uid), None) if uid else None
        if binding is None and strategy in INTERNAL_MAKER_STRATEGIES and market.product_type == "PERP":
            from app.services.user_uid import next_uid_for_role
            user = await session.get(User, uid) if uid else None
            if user is None:
                uid = await next_uid_for_role(session, 'mm_bot')
                user = User(id=uid, username=f'robot_{uid}', username_normalized=f'robot_{uid}', role='mm_bot', is_active=True)
                session.add(user)
                await session.flush()
            binding = MarketBotAccount(market_id=market.id, user_id=user.id, role=role,
                strategy_role='CONTRACT_LADDER', bot_label=f'execution_{user.id}', initial_quote_amount=0,
                initial_base_amount=0, initial_base_notional=0, reference_price=market.reference_price or 1, is_enabled=True)
            session.add(binding)
        if binding is None:
            reference = Decimal(str(market.reference_price or 1))
            binding = await create_market_bot_account(session, market, MarketBotCreateRequest(
                uid=uid, role=role, strategy_role='CONTRACT_LADDER' if strategy in INTERNAL_MAKER_STRATEGIES else role,
                initial_quote_amount=Decimal('100000000'),
                initial_base_notional=Decimal('100000000') if market.product_type == 'SPOT' else Decimal(0),
                reference_price=reference, is_enabled=True,
            ), index=len(existing)+len(chosen)+1)
        if (binding.strategy_role == 'CONTRACT_LADDER') != (strategy in INTERNAL_MAKER_STRATEGIES) and await has_exposure(session, binding.user_id, market.id):
            raise HTTPException(409, f'UID {binding.user_id} 仍有挂单或仓位，不能切换执行权限')
        if strategy not in INTERNAL_MAKER_STRATEGIES or market.product_type == "SPOT":
            from app.core.security import generate_api_key, generate_api_secret
            from app.api.admin import ensure_contract_bot_account, ensure_user_asset_template, ensure_fee_profile
            from datetime import datetime, UTC
            user = await session.get(User, binding.user_id)
            user.api_key = user.api_key or generate_api_key('strategy')
            user.api_secret_hash = user.api_secret_hash or generate_api_secret()
            # Provision missing wallet once; never replenish an existing depleted wallet on save.
            if Decimal(binding.initial_quote_amount or 0) <= 0:
                binding.initial_quote_amount = Decimal('100000000')
            now = datetime.now(UTC)
            if market.product_type == 'PERP':
                await ensure_contract_bot_account(session, user, market=market, margin_asset=market.margin_asset or market.quote_asset,
                    wallet_balance=binding.initial_quote_amount, note='strategy_account_initialization', now=now)
            else:
                await ensure_user_asset_template(session, user, market.quote_asset, binding.initial_quote_amount,
                    note='strategy_account_initialization', now=now)
                await ensure_user_asset_template(session, user, market.base_asset, binding.initial_base_amount,
                    note='strategy_account_initialization', now=now)
            from app.models.fee_profile import FeeProfile
            if await session.scalar(select(FeeProfile.id).where(FeeProfile.user_id == user.id, FeeProfile.market_id == market.id)) is None:
                await ensure_fee_profile(session, user.id, market.id, market.default_maker_fee_rate, market.default_taker_fee_rate)
        # Changing algorithm does not change UID. Internal privileges live on binding.
        binding.strategy_role = 'CONTRACT_LADDER' if strategy in INTERNAL_MAKER_STRATEGIES else role
        binding.is_enabled = True
        chosen.append(binding)
    for binding in existing:
        if binding not in chosen and binding.is_enabled:
            if await has_exposure(session, binding.user_id, market.id):
                raise HTTPException(409, f'UID {binding.user_id} 仍有挂单或仓位，不能解除执行绑定')
            binding.is_enabled = False
    await session.flush()
    return [b.user_id for b in chosen]


async def retire_legacy_presets(session):
    """Retain ledger identities; retire unused presets, never delete account facts."""
    shared = await session.scalar(select(User).where(User.username == 'paper_market_maker'))
    if shared:
        for b in (await session.scalars(select(MarketBotAccount).where(MarketBotAccount.user_id == shared.id))).all():
            b.is_enabled = False
        # Keep the identity usable for historical position/ledger administration.
        if not await has_exposure(session, shared.id):
            shared.is_active = False
    names = [f'spot_mm_{i}' for i in range(1,11)] + ['flow_user_1','flow_user_2']
    for user in (await session.scalars(select(User).where(User.username.in_(names), User.role == 'mm_bot'))).all():
        bound = await session.scalar(select(MarketBotAccount.id).where(MarketBotAccount.user_id == user.id).limit(1))
        if bound is None and not await has_exposure(session, user.id):
            user.is_active = False
