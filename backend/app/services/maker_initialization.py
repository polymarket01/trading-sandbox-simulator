"""Idempotent strategy selection and execution-account provisioning. Never starts trading."""
from copy import deepcopy
from fastapi import HTTPException
from sqlalchemy import select
from app.models.market_strategy_config import MarketStrategyConfig
from app.services.maker_plugins import INTERNAL_MAKER_STRATEGIES, internal_default, get_plugin
from app.services.strategy_accounts import allocate_accounts
from app.services.strategy_config_service import ensure_strategy_templates, selected_market_strategy_config, set_selected_market_strategy


async def ensure_maker_initialized(session, market, *, strategy_key=None, actor_id=None):
    templates = await ensure_strategy_templates(session)
    if strategy_key is None:
        selected = await selected_market_strategy_config(session, market)
        strategy_key = selected.strategy_key
    try:
        plugin = get_plugin(strategy_key)
    except ValueError as exc:
        raise HTTPException(409, "未安装所选铺单策略，不能启动") from exc
    if market.product_type not in plugin.manifest["products"]:
        raise ValueError("策略不支持该产品")
    document = None
    if strategy_key in INTERNAL_MAKER_STRATEGIES:
        row = await session.scalar(select(MarketStrategyConfig).where(
            MarketStrategyConfig.market_id == market.id, MarketStrategyConfig.strategy_key == strategy_key))
        document = deepcopy(row.config_json) if row and row.config_json.get('config') else None
        if document is None:
            source = market.price_source_symbol or market.symbol.removesuffix('-PERP')
            config = internal_default(strategy_key, source)
            if strategy_key == 'SIMPLE_BBO':
                config.update(templates[strategy_key].default_config_json)
            document = {'desired_version': 1, 'config': config}
    uids = await allocate_accounts(session, market, 'maker', strategy_key)
    await set_selected_market_strategy(session, market, strategy_key,
        config_json=document, updated_by_user_id=actor_id)
    await session.flush()
    return {'strategy_key': strategy_key, 'uids': uids}
