"""One-way retirement of legacy configuration; never creates orders or accounts."""
from sqlalchemy import update

from app.models.market_bot_account import MarketBotAccount
from app.models.paper_exchange import PaperLiquidityConfig


async def retire_legacy_configuration(session) -> dict[str, int]:
    configs = await session.execute(
        update(PaperLiquidityConfig)
        .where(PaperLiquidityConfig.enabled.is_(True) | PaperLiquidityConfig.flow_enabled.is_(True))
        .values(enabled=False, flow_enabled=False)
    )
    bindings = await session.execute(
        update(MarketBotAccount)
        .where(MarketBotAccount.strategy_role == "paper_default", MarketBotAccount.is_enabled.is_(True))
        .values(is_enabled=False)
    )
    return {"configs_disabled": configs.rowcount, "bindings_disabled": bindings.rowcount}
