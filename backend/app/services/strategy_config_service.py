from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market import Market
from app.models.market_strategy_config import MarketStrategyConfig
from app.models.strategy_template import StrategyTemplate



from app.services.maker_plugins import strategy_choices, installed_plugins

SPOT_STRATEGY_KEYS = tuple(strategy_choices("SPOT"))
PERP_STRATEGY_KEYS = tuple(strategy_choices("PERP"))
SUPPORTED_STRATEGY_KEYS = tuple(dict.fromkeys((*SPOT_STRATEGY_KEYS, *PERP_STRATEGY_KEYS)))

def _legacy_markers(folder):
    import json
    path = Path(__file__).resolve().parents[1] / "maker_strategies" / folder / "legacy_markers.json"
    return tuple(json.loads(path.read_text())) if path.is_file() else ()


LEGACY_AGGRESSIVE_LITE_DEFAULT_MARKER_SETS = _legacy_markers("lite")
LEGACY_AGGRESSIVE_PERP_MM_DEFAULT_MARKER_SETS = _legacy_markers("perp_mm")
def normalize_strategy_key(value: str | None, default: str = "LITE") -> str:
    key = str(value or default).upper()
    if key == "NONE":
        return key
    if key in SUPPORTED_STRATEGY_KEYS:
        return key
    if value:
        return key  # Preserve unavailable identity for status; execution checks the registry.
    return default if default in SUPPORTED_STRATEGY_KEYS else next(iter(SUPPORTED_STRATEGY_KEYS), "NONE")


def default_config_for_strategy(strategy_key: str) -> dict[str, Any]:
    import json
    from app.services.maker_plugins import get_plugin
    if strategy_key == "NONE":
        return {}
    plugin = get_plugin(strategy_key)
    path = plugin.directory / "defaults.json"
    return json.loads(path.read_text()) if path.exists() else {}


def merge_config(base: dict[str, Any] | None, override: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(base) if isinstance(base, dict) else {}
    if not isinstance(override, dict):
        return merged
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _same_scalar_config_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            return int(actual) == expected
        except (TypeError, ValueError):
            return False
    return str(actual) == str(expected)


def _is_legacy_aggressive_lite_config(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(
        all(
            key in payload and _same_scalar_config_value(payload.get(key), expected)
            for key, expected in marker.items()
        )
        for marker in LEGACY_AGGRESSIVE_LITE_DEFAULT_MARKER_SETS
    )


def is_legacy_aggressive_lite_config(payload: Any) -> bool:
    return _is_legacy_aggressive_lite_config(payload)


def _is_legacy_aggressive_perp_mm_config(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return any(
        all(
            key in payload and _same_scalar_config_value(payload.get(key), expected)
            for key, expected in marker.items()
        )
        for marker in LEGACY_AGGRESSIVE_PERP_MM_DEFAULT_MARKER_SETS
    )


def repair_legacy_strategy_config(item: MarketStrategyConfig) -> bool:
    if item.strategy_key == "LITE":
        should_repair = _is_legacy_aggressive_lite_config(item.config_json)
    elif item.strategy_key == "PERP_MM":
        should_repair = _is_legacy_aggressive_perp_mm_config(item.config_json)
    else:
        should_repair = False
    if not should_repair:
        return False
    item.config_json = {}
    return True












def config_schema_for_strategy(strategy_key: str, default_config: dict[str, Any]) -> dict[str, Any]:
    import json
    from app.services.maker_plugins import get_plugin
    plugin = get_plugin(strategy_key)
    path = plugin.directory / "config_schema.json"
    return json.loads(path.read_text()) if path.exists() else {"strategy_key": plugin.key, "sections": []}


def default_strategy_templates_payload() -> list[dict[str, Any]]:
    return [{"strategy_key": key, "display_name": p.manifest["display_name"], "scope": "market",
             "config_schema_json": config_schema_for_strategy(key, {}),
             "default_config_json": default_config_for_strategy(key), "is_active": True}
            for key, p in installed_plugins().items()]


async def ensure_strategy_templates(session: AsyncSession) -> dict[str, StrategyTemplate]:
    existing_rows = await session.execute(select(StrategyTemplate))
    templates = {item.strategy_key: item for item in existing_rows.scalars()}
    for payload in default_strategy_templates_payload():
        key = payload["strategy_key"]
        template = templates.get(key)
        if template is None:
            template = StrategyTemplate(**payload)
            session.add(template)
            await session.flush()
            templates[key] = template
            continue
        template.display_name = payload["display_name"]
        template.scope = payload["scope"]
        template.config_schema_json = payload["config_schema_json"]
        if key != 'SIMPLE_BBO' or not template.default_config_json:
            template.default_config_json = payload["default_config_json"]
        template.is_active = payload["is_active"]
    return {key: template for key, template in templates.items() if key in installed_plugins()}


async def ensure_market_strategy_config(
    session: AsyncSession,
    market: Market,
    strategy_key: str,
    *,
    is_enabled: bool = False,
    updated_by_user_id: int | None = None,
) -> MarketStrategyConfig:
    key = normalize_strategy_key(strategy_key)
    if key == "NONE":
        return MarketStrategyConfig(market_id=market.id, strategy_key="NONE", config_json={}, is_enabled=False)

    item = await session.scalar(
        select(MarketStrategyConfig).where(
            MarketStrategyConfig.market_id == market.id,
            MarketStrategyConfig.strategy_key == key,
        )
    )
    if item is None:
        item = MarketStrategyConfig(
            market_id=market.id,
            strategy_key=key,
            config_json={},
            is_enabled=is_enabled,
            updated_by_user_id=updated_by_user_id,
        )
        session.add(item)
        await session.flush()
    elif is_enabled and not item.is_enabled:
        item.is_enabled = True
        item.updated_by_user_id = updated_by_user_id
    repair_legacy_strategy_config(item)
    return item


async def ensure_market_strategy_configs(
    session: AsyncSession,
    markets: dict[str, Market],
    *,
    selected_by_symbol: dict[str, str] | None = None,
) -> None:
    await ensure_strategy_templates(session)
    selected_by_symbol = selected_by_symbol or {}
    for symbol, market in markets.items():
        choices = strategy_choices(market.product_type)
        rows = (await session.scalars(select(MarketStrategyConfig).where(
            MarketStrategyConfig.market_id == market.id))).all()
        enabled = [row for row in rows if row.is_enabled]
        if enabled:
            # Keep unavailable selections visible; never re-enable another algorithm.
            continue
        preferred = selected_by_symbol.get(symbol)
        if preferred and preferred not in choices:
            continue
        key = preferred or next(iter(choices), "NONE")
        if key in ("LITE", "PERP_MM"):
            await ensure_market_strategy_config(session, market, key, is_enabled=True)


async def selected_market_strategy_config(
    session: AsyncSession,
    market: Market,
    *,
    fallback_strategy_key: str = "LITE",
) -> MarketStrategyConfig:
    choices = strategy_choices(market.product_type)
    selected = await session.scalar(select(MarketStrategyConfig).where(
        MarketStrategyConfig.market_id == market.id, MarketStrategyConfig.is_enabled.is_(True)))
    if selected is not None:
        if selected.strategy_key not in choices:
            # Stale selection remains disabled in runtime; do not instantiate a substitute.
            return MarketStrategyConfig(market_id=market.id, strategy_key="NONE", config_json={}, is_enabled=False)
        repair_legacy_strategy_config(selected)
        return selected
    if not choices:
        return MarketStrategyConfig(market_id=market.id, strategy_key="NONE", config_json={}, is_enabled=False)
    preferred = "PERP_MM" if market.product_type == "PERP" else "LITE"
    key = fallback_strategy_key if fallback_strategy_key in choices else preferred if preferred in choices else choices[0]
    return await ensure_market_strategy_config(session, market, key, is_enabled=True)


async def set_selected_market_strategy(
    session: AsyncSession,
    market: Market,
    strategy_key: str,
    *,
    config_json: dict[str, Any] | None = None,
    updated_by_user_id: int | None = None,
) -> MarketStrategyConfig:
    key = normalize_strategy_key(strategy_key)
    rows = await session.execute(select(MarketStrategyConfig).where(MarketStrategyConfig.market_id == market.id))
    for item in rows.scalars():
        item.is_enabled = item.strategy_key == key
    selected = await ensure_market_strategy_config(
        session,
        market,
        key,
        is_enabled=True,
        updated_by_user_id=updated_by_user_id,
    )
    if config_json is not None:
        selected.config_json = deepcopy(config_json) if isinstance(config_json, dict) else {}
    selected.updated_by_user_id = updated_by_user_id
    return selected


async def strategy_template_map(session: AsyncSession) -> dict[str, StrategyTemplate]:
    await ensure_strategy_templates(session)
    rows = await session.execute(select(StrategyTemplate))
    return {item.strategy_key: item for item in rows.scalars() if item.strategy_key in installed_plugins()}
