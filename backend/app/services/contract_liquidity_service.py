from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    POSITION_ACTION_OPEN,
    PRODUCT_TYPE_PERP,
    ROLE_BOT,
    SIDE_BUY,
    TIF_GTC,
)
from app.core.decimal_utils import decimal_to_str, to_decimal
from app.core.security import generate_api_key, generate_api_secret, hash_password
from app.core.time_utils import to_millis
from app.models.contract_account import ContractAccount
from app.models.market_maker_instance import MarketMakerInstance
from app.models.market import Market
from app.models.order import Order
from app.models.user import User
from app.schemas.api import ContractOrderCreateRequest, SeedMarketBookRequest
from app.services.contract_price_service import ContractPriceService
from app.services.contract_service import ContractService, ContractValidationError
from app.services.runtime import AppRuntime
from app.services.seed_book import build_seed_book_plan
from app.services.user_uid import next_uid_for_role


DEFAULT_CONTRACT_LIQUIDITY_PASSWORD = "contract-mm123"
MAKER_HEARTBEAT_STALE_SECONDS = 45


def contract_liquidity_username(symbol: str, side: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in symbol).strip("_")
    safe_symbol = (normalized or "market")[:44]
    return f"contract_liq_{safe_symbol}_{side}"


async def ensure_contract_liquidity_user(session: AsyncSession, market: Market, side: str) -> tuple[User, bool]:
    username = contract_liquidity_username(market.symbol, side)
    user = await session.scalar(select(User).where(User.username == username))
    if user is not None:
        if user.role != ROLE_BOT:
            raise ContractValidationError(f"contract liquidity username {username} is not a bot account")
        user.is_active = True
        if not user.api_key:
            user.api_key = generate_api_key(username.replace("_", "-")[:18] or "contract-liq")
        if not user.api_secret_hash:
            user.api_secret_hash = generate_api_secret()
        if not user.password_hash:
            user.password_hash = hash_password(DEFAULT_CONTRACT_LIQUIDITY_PASSWORD)
        return user, False

    user = User(
        id=await next_uid_for_role(session, ROLE_BOT),
        username=username,
        role=ROLE_BOT,
        api_key=generate_api_key(username.replace("_", "-")[:18] or "contract-liq"),
        api_secret_hash=generate_api_secret(),
        password_hash=hash_password(DEFAULT_CONTRACT_LIQUIDITY_PASSWORD),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user, True


class ContractLiquidityService:
    def __init__(
        self,
        runtime: AppRuntime,
        contract_service: ContractService,
        price_service: ContractPriceService,
    ) -> None:
        self.runtime = runtime
        self.contract_service = contract_service
        self.price_service = price_service

    async def cancel_liquidity_orders_for_market(
        self,
        session: AsyncSession,
        market: Market,
        liquidity_users: tuple[User, User],
    ) -> int:
        user_by_id = {user.id: user for user in liquidity_users}
        rows = await session.execute(
            select(Order).where(
                Order.market_id == market.id,
                Order.product_type == PRODUCT_TYPE_PERP,
                Order.user_id.in_(list(user_by_id)),
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            )
        )
        order_ids_by_user: list[tuple[User, str]] = [(user_by_id[order.user_id], str(order.order_id)) for order in rows.scalars()]
        for user, order_id in order_ids_by_user:
            await self.contract_service.cancel_order(session, user, order_id)
        return len(order_ids_by_user)

    async def seed_market_book(
        self,
        session: AsyncSession,
        market: Market,
        payload: SeedMarketBookRequest,
        *,
        now: datetime | None = None,
    ) -> dict:
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ContractValidationError("market is not a PERP contract")
        plan = build_seed_book_plan(market, payload)
        if not plan:
            raise ContractValidationError("seed plan is empty; mid_price is too close to zero")
        if payload.dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "symbol": market.symbol,
                "planned_orders": len(plan),
                "preview": [
                    {
                        "side": item["side"],
                        "price": decimal_to_str(item["price"]),
                        "quantity": decimal_to_str(item["quantity"]),
                        "position_action": POSITION_ACTION_OPEN,
                    }
                    for item in plan[:10]
                ],
            }
        if not payload.confirm_execute:
            raise ContractValidationError("confirm_execute is required when dry_run is false")
        if not market.is_active:
            raise ContractValidationError("market is inactive")

        bid_user, bid_created = await ensure_contract_liquidity_user(session, market, "bid")
        ask_user, ask_created = await ensure_contract_liquidity_user(session, market, "ask")
        canceled_orders = 0
        if payload.cancel_existing:
            canceled_orders = await self.cancel_liquidity_orders_for_market(session, market, (bid_user, ask_user))

        placed_orders = 0
        rejected_orders = 0
        now = now or datetime.now(tz=UTC)
        for index, item in enumerate(plan, start=1):
            user = bid_user if item["side"] == SIDE_BUY else ask_user
            response = await self.contract_service.place_order(
                session,
                user,
                ContractOrderCreateRequest(
                    symbol=market.symbol,
                    side=item["side"],
                    type="limit",
                    tif=TIF_GTC,
                    price=item["price"],
                    quantity=item["quantity"],
                    position_action=POSITION_ACTION_OPEN,
                    reduce_only=False,
                    client_order_id=f"contract-liquidity-{market.symbol}-{to_millis(now)}-{index}",
                ),
                now=now,
            )
            order = response.get("order", {})
            if order.get("status") == "rejected":
                rejected_orders += 1
            else:
                placed_orders += 1

        price_state = await self.price_service.serialize_market_state(session, market, fetch_external=False)
        await session.commit()
        return {
            "ok": rejected_orders == 0,
            "symbol": market.symbol,
            "product_type": PRODUCT_TYPE_PERP,
            "placed_orders": placed_orders,
            "rejected_orders": rejected_orders,
            "canceled_orders": canceled_orders,
            "cancel_existing": payload.cancel_existing,
            "dry_run": False,
            "confirm_execute": True,
            "price_state": price_state,
            "accounts": {
                "bid": {"uid": bid_user.id, "username": bid_user.username, "created": bid_created},
                "ask": {"uid": ask_user.id, "username": ask_user.username, "created": ask_created},
            },
        }

    def mid_price_from_state(self, market: Market, price_state: dict | None) -> Decimal:
        candidates = []
        if price_state:
            candidates.extend(
                [
                    price_state.get("index_price"),
                    price_state.get("external_mark_price"),
                    price_state.get("mark_price"),
                    price_state.get("local_mid_price"),
                ]
            )
        candidates.append(market.reference_price)
        for value in candidates:
            price = to_decimal(value)
            if price > Decimal("0"):
                return price
        raise ContractValidationError(f"no valid mid price for {market.symbol}")

    async def run_once(
        self,
        session: AsyncSession,
        *,
        levels: int,
        gap_ticks: int,
        quantity: Decimal | None,
        now: datetime | None = None,
    ) -> dict:
        now = now or datetime.now(tz=UTC)
        rows = await session.execute(
            select(Market).where(Market.product_type == PRODUCT_TYPE_PERP, Market.is_active.is_(True)).order_by(Market.symbol.asc())
        )
        metrics: dict = {
            "updated_at": to_millis(now),
            "markets": 0,
            "reseeded_markets": 0,
            "skipped_markets": 0,
            "placed_orders": 0,
            "canceled_orders": 0,
            "rejected_orders": 0,
            "errors": [],
            "items": [],
        }
        for market in rows.scalars():
            metrics["markets"] += 1
            try:
                if await self.managed_maker_instance_active(session, market, now=now):
                    metrics["skipped_markets"] += 1
                    metrics["items"].append(
                        {
                            "symbol": market.symbol,
                            "status": "managed_instance_running",
                            "mid_price": None,
                            "placed_orders": 0,
                            "canceled_orders": 0,
                            "rejected_orders": 0,
                        }
                    )
                    continue
                price_state = await self.price_service.serialize_market_state(session, market, fetch_external=False)
                mid_price = self.mid_price_from_state(market, price_state)
                result = await self.seed_market_book(
                    session,
                    market,
                    SeedMarketBookRequest(
                        mid_price=mid_price,
                        levels=levels,
                        gap_ticks=gap_ticks,
                        quantity=quantity,
                        cancel_existing=True,
                        dry_run=False,
                        confirm_execute=True,
                    ),
                    now=now,
                )
                metrics["reseeded_markets"] += 1
                metrics["placed_orders"] += int(result["placed_orders"])
                metrics["canceled_orders"] += int(result["canceled_orders"])
                metrics["rejected_orders"] += int(result["rejected_orders"])
                metrics["items"].append(
                    {
                        "symbol": market.symbol,
                        "mid_price": decimal_to_str(mid_price),
                        "placed_orders": result["placed_orders"],
                        "canceled_orders": result["canceled_orders"],
                        "rejected_orders": result["rejected_orders"],
                    }
                )
            except Exception as exc:
                await session.rollback()
                metrics["errors"].append({"symbol": market.symbol, "error": exc.__class__.__name__})
        self.runtime.contract_liquidity_metrics = metrics
        return metrics

    async def managed_maker_instance_active(
        self,
        session: AsyncSession,
        market: Market,
        *,
        now: datetime,
    ) -> bool:
        instance = await session.scalar(select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id))
        if instance is None or instance.status not in {"starting", "running", "switching", "rollback"}:
            return False
        heartbeat = instance.last_heartbeat_at or instance.started_at
        if heartbeat is None:
            return instance.status in {"starting", "running"}
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)
        return (now - heartbeat).total_seconds() <= MAKER_HEARTBEAT_STALE_SECONDS

    def serialize_runtime_status(self) -> dict:
        return self.runtime.contract_liquidity_metrics
