"""Server-owned permissions for resting maker orders; FLOW never inherits them."""
from sqlalchemy import select
from app.models.market_bot_account import MarketBotAccount
from app.models.user import User


async def min_notional_exempt(session, user, market, order) -> bool:
    if getattr(order, 'type', None) != 'limit' or getattr(order, 'tif', None) not in ('gtc', 'post_only'):
        return False
    roles = set((await session.scalars(select(MarketBotAccount.role).join(User, User.id == MarketBotAccount.user_id).where(
        MarketBotAccount.user_id == user.id, MarketBotAccount.market_id == market.id,
        MarketBotAccount.is_enabled.is_(True), User.is_active.is_(True), User.role == 'mm_bot',
    ))).all())
    return 'maker' in roles and 'flow' not in roles
