from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ORDER_STATUS_NEW,
    ORDER_STATUS_PARTIALLY_FILLED,
    POSITION_MODE_HEDGE,
    PRODUCT_TYPE_PERP,
    PRODUCT_TYPE_SPOT,
    DEFAULT_PAPER_ACCOUNT_USDT,
    ROLE_ADMIN,
    ROLE_BOT,
    ROLE_MANUAL,
    SIDE_BUY,
    SIDE_SELL,
    TIF_GTC,
    ZERO,
)
from app.core.config import settings
from app.core.decimal_utils import quantize_step
from app.core.security import generate_api_key, generate_api_secret, hash_password
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_user_setting import ContractUserSetting
from app.models.display_kline import DisplayKline
from app.models.fee_profile import FeeProfile
from app.models.kline import Kline
from app.models.ledger_entry import LedgerEntry
from app.models.market import MARKET_VISIBILITY_LISTED, MARKET_VISIBILITY_TEST, Market
from app.models.market_bot_account import MarketBotAccount
from app.models.order import Order
from app.models.reset_template import ResetTemplate
from app.models.trade import Trade
from app.models.user import User
from app.models.paper_exchange import (
    PaperAsset,
    PaperBrandConfig,
    PaperGlobalRun,
    PaperLiquidityConfig,
    PaperSystemSetting,
)
from app.schemas.api import OrderCreateRequest
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account
from app.services.account_service import AccountService
from app.services.order_service import OrderService
from app.services.runtime import AppRuntime
from app.services.persistence_contract import legacy_sampled_runtime, platform_durable_contract
from app.services.paper_account_service import (
    ensure_global_run,
    ensure_user_assets,
    normalize_username,
    paper_maker_inventory_target,
)
from app.services.strategy_config_service import ensure_market_strategy_configs
from app.services.user_uid import next_uid_for_role


BOT_USERS = [
    (f"spot_mm_{index}", ROLE_BOT, f"mm{index}-demo-key", f"mm{index}-demo-secret")
    for index in range(1, 11)
]

FLOW_USERS = [
    ("flow_user_1", ROLE_BOT, "flow1-demo-key", "flow1-demo-secret"),
    ("flow_user_2", ROLE_BOT, "flow2-demo-key", "flow2-demo-secret"),
]

TRADER_USERS = [
    (f"trader{index:02d}", ROLE_MANUAL, f"trader{index:02d}-demo-key", f"trader{index:02d}-demo-secret")
    for index in range(1, 11)
]

USERS = [
    ("spot_manual_user", ROLE_MANUAL, "manual-demo-key", "manual-demo-secret"),
    *TRADER_USERS,
    *BOT_USERS,
    *FLOW_USERS,
    ("spot_admin", ROLE_ADMIN, "admin-demo-key", "admin-demo-secret"),
]

DEFAULT_PASSWORDS = {
    "spot_manual_user": "manual123",
    "spot_admin": "admin123",
    **{f"trader{index:02d}": f"trader{index:02d}" for index in range(1, 11)},
    **{f"spot_mm_{index}": "mm123" for index in range(1, 11)},
    "flow_user_1": "flow123",
    "flow_user_2": "flow123",
}

DEFAULT_MARKET_BOT_PASSWORD = "mm123"
DEFAULT_MARKET_BOT_COUNT = 2
DEFAULT_MARKET_FLOW_BOT_COUNT = 1
DEFAULT_MARKET_BOT_QUOTE_AMOUNT = Decimal("10000000000")
DEFAULT_MARKET_BOT_BASE_NOTIONAL = Decimal("10000000000")
DEFAULT_PERP_MARKET_BOT_QUOTE_AMOUNT = Decimal("10000000000")
DEFAULT_PERP_MARKET_BOT_BASE_NOTIONAL = Decimal("0")
DEFAULT_MARKET_BOT_MAKER_FEE = Decimal("0")
DEFAULT_MARKET_BOT_TAKER_FEE = Decimal("0")
# 5198 PaperTrading 外部策略 bot 预置（与 5174 同款策略参数配合使用）。
PAPER_STRATEGY_MAKER_COUNT = 2
PAPER_STRATEGY_FLOW_COUNT = 1
PAPER_STRATEGY_BOT_QUOTE_AMOUNT = Decimal("1000000000")
PAPER_STRATEGY_BOT_BASE_NOTIONAL = Decimal("1000000000")
PAPER_STRATEGY_BOT_PASSWORD = "paper-bot"

MARKETS = [
    {
        "symbol": "BTCUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "mainstream",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.01"),
        "qty_step": Decimal("0.0001"),
        "min_qty": Decimal("0.0001"),
        "min_notional": Decimal("5"),
        "price_precision": 2,
        "qty_precision": 4,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "BTCUSDT-PERP",
        "product_type": PRODUCT_TYPE_PERP,
        "market_type": "mainstream",
        "base_asset": "BTC",
        "quote_asset": "USDT",
        "margin_asset": "USDT",
        "price_tick": Decimal("0.01"),
        "qty_step": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "max_leverage": Decimal("20"),
        "default_leverage": Decimal("5"),
        "maintenance_margin_rate": Decimal("0.005"),
        "funding_rate": Decimal("0"),
        "funding_interval_hours": 8,
        "index_price_source": "binance",
        "mark_price_mode": "orderbook",
        "funding_rate_mode": "binance",
        "funding_interest_rate": Decimal("0.0001"),
        "funding_clamp_rate": Decimal("0.0005"),
        "funding_cap_rate": Decimal("0.02"),
        "funding_impact_notional": Decimal("25000"),
        "contract_trading_mode": "normal",
        "price_precision": 2,
        "qty_precision": 3,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "ETHUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "mainstream",
        "base_asset": "ETH",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.01"),
        "qty_step": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "price_precision": 2,
        "qty_precision": 3,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "SOLUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "mainstream",
        "base_asset": "SOL",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 3,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "DOGEUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "mainstream",
        "base_asset": "DOGE",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.00001"),
        "qty_step": Decimal("1"),
        "min_qty": Decimal("1"),
        "min_notional": Decimal("5"),
        "price_precision": 5,
        "qty_precision": 0,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "XXXUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "listed",
        "base_asset": "XXX",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 3,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "YYYUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "listed",
        "base_asset": "YYY",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 3,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
    {
        "symbol": "ZZZUSDT",
        "product_type": PRODUCT_TYPE_SPOT,
        "market_type": "listed",
        "base_asset": "ZZZ",
        "quote_asset": "USDT",
        "price_tick": Decimal("0.0001"),
        "qty_step": Decimal("0.01"),
        "min_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "price_precision": 4,
        "qty_precision": 2,
        "default_maker_fee_rate": Decimal("0"),
        "default_taker_fee_rate": Decimal("0"),
    },
]

DEFAULT_TEST_ASSETS = {
    "USDT": DEFAULT_PAPER_ACCOUNT_USDT,
    "BTC": Decimal("100000000"),
    "ETH": Decimal("100000000"),
    "SOL": Decimal("100000000"),
    "DOGE": Decimal("100000000"),
    "XXX": Decimal("100000000"),
    "YYY": Decimal("100000000"),
    "ZZZ": Decimal("100000000"),
}

RESET_AMOUNTS = {
    username: DEFAULT_TEST_ASSETS.copy()
    for username, role, _, _ in USERS
    if role in {ROLE_ADMIN, ROLE_MANUAL, ROLE_BOT}
}
for username, _, _, _ in TRADER_USERS:
    RESET_AMOUNTS[username] = {
        asset: DEFAULT_PAPER_ACCOUNT_USDT if asset == "USDT" else ZERO
        for asset in DEFAULT_TEST_ASSETS
    }
RESET_AMOUNTS["spot_admin"] = {"USDT": DEFAULT_PAPER_ACCOUNT_USDT}

MARKET_SEEDS = {
    "BTCUSDT": {
        "base_price": Decimal("62000"),
        "book_gap": Decimal("5"),
        "book_qty_base": Decimal("0.12"),
        "book_qty_step": Decimal("0.01"),
        "history_tick": Decimal("0.01"),
        "history_qty_base": Decimal("0.08"),
        "history_qty_step": Decimal("0.01"),
    },
    "ETHUSDT": {
        "base_price": Decimal("3400"),
        "book_gap": Decimal("0.5"),
        "book_qty_base": Decimal("2.4"),
        "book_qty_step": Decimal("0.2"),
        "history_tick": Decimal("0.2"),
        "history_qty_base": Decimal("1.8"),
        "history_qty_step": Decimal("0.1"),
    },
    "SOLUSDT": {
        "base_price": Decimal("150"),
        "book_gap": Decimal("0.02"),
        "book_qty_base": Decimal("45"),
        "book_qty_step": Decimal("2"),
        "history_tick": Decimal("0.01"),
        "history_qty_base": Decimal("35"),
        "history_qty_step": Decimal("2"),
    },
    "DOGEUSDT": {
        "base_price": Decimal("0.16000"),
        "book_gap": Decimal("0.00005"),
        "book_qty_base": Decimal("50000"),
        "book_qty_step": Decimal("1500"),
        "history_tick": Decimal("0.00002"),
        "history_qty_base": Decimal("42000"),
        "history_qty_step": Decimal("1500"),
    },
    "XXXUSDT": {
        "base_price": Decimal("12"),
        "book_gap": Decimal("0.01"),
        "book_qty_base": Decimal("800"),
        "book_qty_step": Decimal("30"),
        "history_tick": Decimal("0.001"),
        "history_qty_base": Decimal("600"),
        "history_qty_step": Decimal("25"),
    },
    "YYYUSDT": {
        "base_price": Decimal("4.2"),
        "book_gap": Decimal("0.01"),
        "book_qty_base": Decimal("1500"),
        "book_qty_step": Decimal("45"),
        "history_tick": Decimal("0.001"),
        "history_qty_base": Decimal("1200"),
        "history_qty_step": Decimal("35"),
    },
    "ZZZUSDT": {
        "base_price": Decimal("0.2450"),
        "book_gap": Decimal("0.0005"),
        "book_qty_base": Decimal("5000"),
        "book_qty_step": Decimal("120"),
        "history_tick": Decimal("0.0001"),
        "history_qty_base": Decimal("4200"),
        "history_qty_step": Decimal("90"),
    },
}


DEFAULT_REFERENCE_PRICES = {
    **{symbol: seed["base_price"] for symbol, seed in MARKET_SEEDS.items()},
    "BTCUSDT-PERP": Decimal("62000"),
}


def _default_reference_price(symbol: str) -> Decimal | None:
    return DEFAULT_REFERENCE_PRICES.get(symbol)


async def _bootstrap_paper_exchange(session: AsyncSession, runtime: AppRuntime) -> None:
    """Create the small, self-contained PaperTrading product dataset.

    This branch intentionally does not call the legacy sandbox bootstrap: the
    white-label product must not expose demo traders, old sampled fixtures, or
    legacy credentials as customer accounts.
    """
    selected_symbols = ["BTCUSDT", "ETHUSDT", "BTCUSDT-PERP", "ETHUSDT-PERP"]
    payloads: dict[str, dict] = {
        item["symbol"]: dict(item)
        for item in MARKETS
        if item["symbol"] in {"BTCUSDT", "ETHUSDT", "BTCUSDT-PERP"}
    }
    perp_template = dict(payloads["BTCUSDT-PERP"])
    perp_template.update(
        {
            "symbol": "ETHUSDT-PERP",
            "base_asset": "ETH",
            "reference_price": Decimal("3400"),
        }
    )
    payloads["ETHUSDT-PERP"] = perp_template

    market_rows = await session.execute(select(Market))
    markets_by_symbol = {str(row.symbol).upper(): row for row in market_rows.scalars()}
    paper_markets: list[Market] = []
    for symbol in selected_symbols:
        payload = payloads[symbol]
        market = markets_by_symbol.get(symbol)
        # 主流种子市场默认跟随 Binance 公共行情（现货 spot ticker / 永续
        # premiumIndex 指数价，只读、无密钥）；网络不可用时回退 reference_price
        # 并标记 stale，不会伪造价格。自有币种仍可通过极简上币选择
        # manual/simulated 价格源。
        binance_symbol = symbol.split("-")[0]
        if market is None:
            market = Market(
                **{key: value for key, value in payload.items() if key != "reference_price"},
                reference_price=payload.get("reference_price") or _default_reference_price(symbol),
                is_active=True,
                paper_status="TRADING",
                visibility=MARKET_VISIBILITY_LISTED,
                price_source="binance",
                price_source_symbol=binance_symbol,
            )
            session.add(market)
            await session.flush()
        else:
            market.is_active = True
            market.paper_status = "TRADING"
            market.visibility = MARKET_VISIBILITY_LISTED
            market.price_source = market.price_source or "binance"
            market.price_source_symbol = market.price_source_symbol or binance_symbol
            if market.reference_price is None:
                market.reference_price = payload.get("reference_price") or _default_reference_price(symbol)
        paper_markets.append(market)

    assets = {"USDT", *(str(market.base_asset).upper() for market in paper_markets)}
    for code in sorted(assets):
        asset = await session.scalar(select(PaperAsset).where(PaperAsset.code == code))
        if asset is None:
            session.add(
                PaperAsset(
                    code=code,
                    display_name=code,
                    description="PaperTrading simulated asset",
                    display_precision=8,
                    status="ACTIVE",
                )
            )

    brand = await session.scalar(select(PaperBrandConfig).where(PaperBrandConfig.id == 1))
    if brand is None:
        session.add(
            PaperBrandConfig(
                id=1,
                exchange_name=os.getenv("PAPER_EXCHANGE_NAME", "Paper Exchange"),
                primary_color=os.getenv("PAPER_EXCHANGE_PRIMARY_COLOR", "#22d3ee"),
                default_language="zh-CN",
                footer_text="Paper Trading / 模拟交易",
                paper_notice="所有资产均为模拟资金，不涉及真实资金。",
            )
        )
    global_run = await ensure_global_run(session)
    runtime.paper_global_run_id = global_run.run_id

    async def ensure_user(username: str, role: str, password: str) -> User:
        normalized = normalize_username(username)
        user = await session.scalar(select(User).where(User.username_normalized == normalized))
        if user is None:
            user = await session.scalar(select(User).where(User.username == username))
        if user is None:
            user = User(
                username=username,
                username_normalized=normalized,
                role=role,
                api_key=generate_api_key("paper"),
                api_secret_hash=generate_api_secret(),
                password_hash=hash_password(password),
                is_active=True,
                account_epoch=0,
            )
            session.add(user)
            await session.flush()
        else:
            user.role = role
            user.username_normalized = normalized
            user.is_active = True
            if user.password_hash is None:
                user.password_hash = hash_password(password)
        return user

    admin_password = str(os.getenv("PAPER_EXCHANGE_ADMIN_PASSWORD") or "").strip()
    if not admin_password:
        raise RuntimeError("PAPER_EXCHANGE_ADMIN_PASSWORD must be set for paper_exchange bootstrap")

    admin = await ensure_user(
        os.getenv("PAPER_EXCHANGE_ADMIN_USERNAME", "admin"),
        ROLE_ADMIN,
        admin_password,
    )
    admin.password_hash = hash_password(admin_password)
    # run_sandbox 需要固定管理 API Key 才能通过 /admin 控制面管理 5198 的
    # 策略进程（铺单、FLOW、重启）。有显式注入时每次启动都对齐，避免
    # 首次生成后轮换导致 runner 持有的 key 失效。
    pinned_admin_key = str(os.getenv("PAPER_EXCHANGE_ADMIN_API_KEY") or "").strip()
    if pinned_admin_key and admin.api_key != pinned_admin_key:
        admin.api_key = pinned_admin_key
    # 管理员使用交易终端（X-API-Key 会话）时同样要有可交易的模拟资产：
    # 现货 USDT + base 库存、永续合约钱包。只补足不削减，保证管理员的
    # 成交历史跨重启保留，不会像懒创建路径那样退回 1M 演示钱包。
    await _ensure_paper_admin_assets(session, admin, paper_markets)
    # Accounts are allocated only when a strategy instance is configured.
    await ensure_market_strategy_configs(
        session,
        {str(market.symbol): market for market in paper_markets},
    )

    system_values = {
        "paper_market_symbols": selected_symbols,
        "paper_brand_version": "1.0",
        "paper_global_run_id": runtime.paper_global_run_id,
        "paper_defaults": {
            "spot_initial_usdt": str(settings.paper_exchange_default_spot_usdt),
            "perp_initial_usdt": str(settings.paper_exchange_default_perp_usdt),
            "default_leverage": "5",
            "user_reset_enabled": True,
        },
    }
    for key, value in system_values.items():
        row = await session.scalar(select(PaperSystemSetting).where(PaperSystemSetting.key == key))
        if row is None:
            session.add(PaperSystemSetting(key=key, value_json=value))
        elif key != "paper_defaults":
            # Admin-managed defaults survive a process restart.  Runtime
            # identity keys are refreshed on every bootstrap, but customer
            # configuration is not silently overwritten by seed values.
            row.value_json = value
    await session.flush()


async def _ensure_paper_admin_assets(
    session: AsyncSession,
    admin: User,
    markets: list[Market],
) -> None:
    """Provision trading assets for the Paper admin account (top-up only).

    管理员用 X-API-Key 会话在交易终端下单时，与普通用户一样需要现货
    USDT/base 库存和永续合约钱包。首次启动补足到 Paper 默认额度，之后
    只补足缺口、不削减（保留管理员自己的成交历史）。
    """
    now = datetime.now(tz=UTC)
    account_service = AccountService()
    spot_target = Decimal(str(settings.paper_exchange_default_spot_usdt))
    perp_target = Decimal(str(settings.paper_exchange_default_perp_usdt))
    balance = await account_service.get_balance(session, admin.id, "USDT")
    current = Decimal(balance.available or ZERO) + Decimal(balance.frozen or ZERO)
    if current < spot_target:
        await account_service.apply_change(
            session,
            admin.id,
            "USDT",
            available_delta=spot_target - current,
            frozen_delta=ZERO,
            change_type="paper_initial_balance",
            amount=spot_target - current,
            note="paper_admin_bootstrap",
            created_at=now,
        )
    for market in markets:
        if market.product_type != PRODUCT_TYPE_SPOT:
            continue
        reference = Decimal(market.reference_price or 1)
        inventory_target = paper_maker_inventory_target(
            reference,
            Decimal(market.price_tick or Decimal("0.01")),
            Decimal(market.qty_step or Decimal("0.0001")),
            Decimal("5000000"),
        )
        base_balance = await account_service.get_balance(session, admin.id, market.base_asset)
        base_current = Decimal(base_balance.available or ZERO) + Decimal(base_balance.frozen or ZERO)
        if base_current < inventory_target:
            await account_service.apply_change(
                session,
                admin.id,
                market.base_asset,
                available_delta=inventory_target - base_current,
                frozen_delta=ZERO,
                change_type="paper_initial_balance",
                amount=inventory_target - base_current,
                note=f"paper_admin_bootstrap:{market.symbol}",
                created_at=now,
            )
    perp_markets = [market for market in markets if market.product_type == PRODUCT_TYPE_PERP]
    if perp_markets:
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == admin.id,
                ContractAccount.margin_asset == "USDT",
            )
        )
        if account is None:
            account = ContractAccount(
                user_id=admin.id,
                margin_asset="USDT",
                wallet_balance=perp_target,
                available_margin=perp_target,
                used_margin=ZERO,
                unrealized_pnl=ZERO,
                realized_pnl=ZERO,
                total_fees=ZERO,
                updated_at=now,
            )
            session.add(account)
            await session.flush()
            await add_contract_ledger_entry(
                session,
                account,
                change_type="paper_initial_balance",
                amount=perp_target,
                before={
                    "wallet": ZERO,
                    "available": ZERO,
                    "used_margin": ZERO,
                    "unrealized_pnl": ZERO,
                    "realized_pnl": ZERO,
                    "total_fees": ZERO,
                },
                note="paper_admin_bootstrap",
                created_at=now,
            )
            for market in perp_markets:
                setting = await session.scalar(
                    select(ContractUserSetting).where(
                        ContractUserSetting.user_id == admin.id,
                        ContractUserSetting.market_id == market.id,
                    )
                )
                if setting is None:
                    session.add(
                        ContractUserSetting(
                            user_id=admin.id,
                            market_id=market.id,
                            leverage=market.default_leverage,
                            margin_mode="isolated",
                            position_mode="one_way",
                        )
                    )
        elif Decimal(account.wallet_balance or ZERO) < perp_target:
            # 只补足历史遗留的 1M 演示钱包等低额度，不覆盖已有大额/亏损状态。
            before = snapshot_contract_account(account)
            top_up = perp_target - Decimal(account.wallet_balance or ZERO)
            account.wallet_balance = perp_target
            account.available_margin = Decimal(account.available_margin or ZERO) + top_up
            account.updated_at = now
            await add_contract_ledger_entry(
                session,
                account,
                change_type="paper_initial_balance",
                amount=top_up,
                before=before,
                note="paper_admin_bootstrap",
                created_at=now,
            )


async def _ensure_paper_strategy_bots(session: AsyncSession, market: Market) -> None:
    """Provision external-strategy bot accounts for one PaperTrading market.

    5198 复用 5174 的 LITE / PERP_MM 策略进程，需要一个与内置报价 maker
    分离的机器人账号集合（2 个策略 maker + 1 个 flow）。所有账号沿用
    Paper 的角色语义（role=bot，走 fast path / 报价临时态持久化边界），
    余额按策略演示配置深度预置，避免 100 档分层因余额不足被风控拒绝。
    """
    now = datetime.now(tz=UTC)
    is_perp = market.product_type == PRODUCT_TYPE_PERP
    reference = Decimal(market.reference_price or 1)
    base_amount = (
        ZERO
        if is_perp
        else quantize_step(PAPER_STRATEGY_BOT_BASE_NOTIONAL / reference, Decimal(market.qty_step))
    )

    async def ensure_bot_user(username: str) -> User:
        normalized = normalize_username(username)
        user = await session.scalar(select(User).where(User.username_normalized == normalized))
        if user is None:
            user = User(
                id=await next_uid_for_role(session, ROLE_BOT),
                username=normalized,
                username_normalized=normalized,
                role=ROLE_BOT,
                api_key=generate_api_key(normalized.replace("_", "-")[:18] or "paper-bot"),
                api_secret_hash=generate_api_secret(),
                password_hash=hash_password(PAPER_STRATEGY_BOT_PASSWORD),
                is_active=True,
            )
            session.add(user)
            await session.flush()
        else:
            user.role = ROLE_BOT
            user.is_active = True
            if not user.api_key:
                user.api_key = generate_api_key(normalized.replace("_", "-")[:18] or "paper-bot")
            if user.password_hash is None:
                user.password_hash = hash_password(PAPER_STRATEGY_BOT_PASSWORD)
        return user

    async def ensure_market_bot(user: User, role: str, label: str, index: int) -> MarketBotAccount:
        bot = await session.scalar(
            select(MarketBotAccount).where(
                MarketBotAccount.market_id == market.id,
                MarketBotAccount.user_id == user.id,
                MarketBotAccount.role == role,
            )
        )
        if bot is not None:
            bot.is_enabled = True
            bot.reference_price = reference
            return bot
        bot = MarketBotAccount(
            market_id=market.id,
            user_id=user.id,
            bot_label=label,
            role=role,
            strategy_role="maker" if role == "maker" else "flow",
            initial_quote_amount=PAPER_STRATEGY_BOT_QUOTE_AMOUNT,
            initial_base_amount=base_amount,
            initial_base_notional=ZERO if is_perp else PAPER_STRATEGY_BOT_BASE_NOTIONAL,
            reference_price=reference,
            is_enabled=True,
        )
        session.add(bot)
        await session.flush()
        return bot

    for index in range(1, PAPER_STRATEGY_MAKER_COUNT + 1):
        user = await ensure_bot_user(f"paper_mm_{market.symbol.lower()}_{index}")
        if is_perp:
            await _ensure_market_bot_contract_account(
                session,
                user,
                market,
                market.margin_asset or market.quote_asset,
                PAPER_STRATEGY_BOT_QUOTE_AMOUNT,
                note=f"paper_strategy_maker:{market.symbol}",
                now=now,
            )
        else:
            await _ensure_market_bot_asset(
                session,
                user,
                market.quote_asset,
                PAPER_STRATEGY_BOT_QUOTE_AMOUNT,
                note=f"paper_strategy_maker:{market.symbol}",
                now=now,
                comparison_step=Decimal("0.01"),
            )
            await _ensure_market_bot_asset(
                session,
                user,
                market.base_asset,
                base_amount,
                note=f"paper_strategy_maker:{market.symbol}",
                now=now,
                comparison_step=Decimal(market.qty_step),
            )
        fee = await session.scalar(
            select(FeeProfile).where(FeeProfile.user_id == user.id, FeeProfile.market_id == market.id)
        )
        if fee is None:
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=DEFAULT_MARKET_BOT_MAKER_FEE,
                    taker_fee_rate=DEFAULT_MARKET_BOT_TAKER_FEE,
                )
            )
        await ensure_market_bot(
            user,
            "maker",
            f"paper-strategy-{market.symbol.lower()}-MM-{index}",
            index,
        )

    for index in range(1, PAPER_STRATEGY_FLOW_COUNT + 1):
        # 复用内置 FLOW 使用的用户名，两套 FLOW（内置兜底 / 策略进程）共享
        # 同一 flow 账号但同一时刻只有一个在跑（策略健康时内置 FLOW 让位）。
        user = await ensure_bot_user(f"paper_flow_{market.symbol.lower()}")
        if is_perp:
            await _ensure_market_bot_contract_account(
                session,
                user,
                market,
                market.margin_asset or market.quote_asset,
                PAPER_STRATEGY_BOT_QUOTE_AMOUNT,
                note=f"paper_strategy_flow:{market.symbol}",
                now=now,
            )
        else:
            await _ensure_market_bot_asset(
                session,
                user,
                market.quote_asset,
                PAPER_STRATEGY_BOT_QUOTE_AMOUNT,
                note=f"paper_strategy_flow:{market.symbol}",
                now=now,
                comparison_step=Decimal("0.01"),
            )
            await _ensure_market_bot_asset(
                session,
                user,
                market.base_asset,
                base_amount,
                note=f"paper_strategy_flow:{market.symbol}",
                now=now,
                comparison_step=Decimal(market.qty_step),
            )
        fee = await session.scalar(
            select(FeeProfile).where(FeeProfile.user_id == user.id, FeeProfile.market_id == market.id)
        )
        if fee is None:
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=DEFAULT_MARKET_BOT_MAKER_FEE,
                    taker_fee_rate=DEFAULT_MARKET_BOT_TAKER_FEE,
                )
            )
        await ensure_market_bot(
            user,
            "flow",
            f"paper-strategy-{market.symbol.lower()}-FLOW-{index}",
            index,
        )


async def _bootstrap_test_lab(session: AsyncSession) -> None:
    """策略实验面：test 市场种子 + 机器人账号 + API 测试账号。

    只创建平台上尚不存在的市场符号（不与 listed 市场冲突）；已存在的
    市场保持原 visibility 不动。
    """
    existing = await session.execute(select(Market))
    existing_symbols = {str(row.symbol).upper(): row for row in existing.scalars()}
    test_markets: dict[str, Market] = {}
    for payload in MARKETS:
        symbol = str(payload["symbol"]).upper()
        if symbol in existing_symbols:
            continue
        reference_price = _default_reference_price(symbol)
        market = Market(
            **payload,
            reference_price=reference_price,
            is_active=True,
            visibility=MARKET_VISIBILITY_TEST,
        )
        session.add(market)
        await session.flush()
        test_markets[symbol] = market
    # Existing unified databases already have the test markets.  Keep using
    # them for the account/template upgrade path instead of returning early;
    # otherwise legacy trader/admin users never receive the new perp account.
    for payload in MARKETS:
        symbol = str(payload["symbol"]).upper()
        existing_market = existing_symbols.get(symbol)
        if existing_market is not None and existing_market.visibility == MARKET_VISIBILITY_TEST:
            test_markets.setdefault(symbol, existing_market)
    if not test_markets:
        return

    users = await _ensure_users(session, include_bots=False)
    await ensure_market_strategy_configs(session, test_markets)
    await _ensure_fee_profiles(session, users, test_markets)
    await _ensure_reset_templates(session, users)
    # durable 契约下机器人账号资产写库（与现役 5174 语义一致，只是不再内存）。
    await _ensure_initial_balances(session, users)
    await _ensure_test_customer_accounts(session, users, test_markets)


async def bootstrap(session: AsyncSession, runtime: AppRuntime) -> None:
    if platform_durable_contract():
        # 一体化平台（durable）：白标产品面（listed 市场/用户/品牌）+
        # 策略实验面（test 市场/机器人账号/API 测试账号）同库共存。
        await _bootstrap_paper_exchange(session, runtime)
        await _bootstrap_test_lab(session)
        from app.services.strategy_accounts import retire_legacy_presets
        await retire_legacy_presets(session)
        await session.flush()
        if session.bind.dialect.name == "sqlite":
            from sqlalchemy import text
            await session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_active_strategy_uid ON market_bot_accounts(user_id) WHERE is_enabled = 1"))
        await session.commit()
        return
    users = await _ensure_users(session)
    markets = await _ensure_markets(session)
    await ensure_market_strategy_configs(
        session,
        markets,
        selected_by_symbol=runtime.liquidity_strategy_selection,
    )
    await _ensure_fee_profiles(session, users, markets)
    await _ensure_reset_templates(session, users)
    await _ensure_market_bot_accounts(session, markets)
    await _ensure_initial_balances(session, users)
    await session.commit()

    service = OrderService(runtime)
    trade_count = await session.scalar(select(func.count()).select_from(Trade))
    if not trade_count:
        await session.execute(delete(Balance))
        await session.execute(delete(LedgerEntry))
        await session.execute(delete(Order))
        await session.execute(delete(Trade))
        await session.execute(delete(Kline))
        await session.execute(delete(DisplayKline))
        await session.commit()
        await _ensure_initial_balances(session, users)
        await _ensure_market_bot_accounts(session, markets)
        await session.commit()
        await _seed_trade_history(session, users, service)

    missing_history_symbols = await _symbols_missing_trade_history(session)
    if missing_history_symbols:
        await _seed_trade_history(session, users, service, missing_history_symbols)

    missing_orderbook_symbols = await _symbols_missing_open_orders(session)
    if missing_orderbook_symbols:
        await _seed_orderbook(session, users, service, missing_orderbook_symbols)


async def load_sampled_runtime_defaults(session: AsyncSession, runtime: AppRuntime) -> dict[str, int]:
    """Seed the process-local sampled mirror without writing business facts.

    ``control.db`` contains identities and market bot bindings only in sampled
    mode.  The old bootstrap balances/contract accounts are deliberately not
    read because they would make an old SQLite row authoritative again.
    """

    from app.services.clearinghouse import ContractAccountState, SpotBalanceState

    clearinghouse = runtime.clearinghouse
    clearinghouse._spot.clear()
    clearinghouse._contract.clear()
    clearinghouse._positions.clear()

    users = await session.execute(select(User))
    users_by_name = {str(user.username): user for user in users.scalars()}
    for username, assets in RESET_AMOUNTS.items():
        user = users_by_name.get(username)
        if user is None:
            continue
        for asset, amount in assets.items():
            clearinghouse._spot[(int(user.id), str(asset).upper())] = SpotBalanceState(
                available=Decimal(amount),
                frozen=ZERO,
            )
        # Sampled mode intentionally ignores durable contract_account rows,
        # but the seeded manual sandbox user must still be able to submit a
        # real PERP order against the process-local book.  Keep this strictly
        # in memory and only grant the same default USDT test asset already
        # used by the sampled spot mirror.
        if user.role in {ROLE_ADMIN, ROLE_MANUAL}:
            margin = Decimal(assets.get("USDT", ZERO))
            if margin > ZERO:
                clearinghouse._contract[(int(user.id), "USDT")] = ContractAccountState(
                    wallet_balance=margin,
                )

    rows = await session.execute(
        select(MarketBotAccount, Market).join(Market, Market.id == MarketBotAccount.market_id)
    )
    bot_rows = list(rows.all())
    for bot, market in bot_rows:
        user_id = int(bot.user_id)
        if market.product_type == PRODUCT_TYPE_PERP:
            asset = str(market.margin_asset or market.quote_asset).upper()
            key = (user_id, asset)
            state = clearinghouse._contract.setdefault(key, ContractAccountState())
            state.wallet_balance = max(state.wallet_balance, Decimal(bot.initial_quote_amount or ZERO))
            continue
        quote_key = (user_id, str(market.quote_asset).upper())
        quote_state = clearinghouse._spot.setdefault(quote_key, SpotBalanceState())
        quote_state.available = max(quote_state.available, Decimal(bot.initial_quote_amount or ZERO))
        base_key = (user_id, str(market.base_asset).upper())
        base_state = clearinghouse._spot.setdefault(base_key, SpotBalanceState())
        base_state.available = max(base_state.available, Decimal(bot.initial_base_amount or ZERO))

    return {
        "spot_accounts": len(clearinghouse._spot),
        "contract_accounts": len(clearinghouse._contract),
        "positions": len(clearinghouse._positions),
    }


async def _ensure_users(session: AsyncSession, *, include_bots: bool = True) -> dict[str, User]:
    existing = await session.execute(select(User))
    users = {user.username: user for user in existing.scalars() if include_bots or user.role != ROLE_BOT}
    for username, role, api_key, api_secret in USERS:
        if role == ROLE_BOT and not include_bots:
            continue
        if username in users:
            # Existing demo databases are upgraded in-place; do not rotate API keys.
            if users[username].password_hash is None:
                users[username].password_hash = hash_password(DEFAULT_PASSWORDS[username])
            continue
        user = User(
            id=await next_uid_for_role(session, role),
            username=username,
            role=role,
            api_key=api_key,
            api_secret_hash=api_secret,
            password_hash=hash_password(DEFAULT_PASSWORDS[username]),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        users[username] = user
    return users


async def _ensure_markets(session: AsyncSession) -> dict[str, Market]:
    existing = await session.execute(select(Market))
    markets = {market.symbol: market for market in existing.scalars()}
    for payload in MARKETS:
        symbol = payload["symbol"]
        reference_price = _default_reference_price(symbol)
        existing_market = markets.get(symbol)
        if existing_market is not None:
            existing_market.product_type = payload.get("product_type", PRODUCT_TYPE_SPOT)
            existing_market.market_type = payload["market_type"]
            # 沙盒 seed 市场恒为实验市场（一体化平台：test 市场用户端不可见）。
            existing_market.visibility = MARKET_VISIBILITY_TEST
            existing_market.base_asset = payload["base_asset"]
            existing_market.quote_asset = payload["quote_asset"]
            existing_market.margin_asset = payload.get("margin_asset")
            if existing_market.reference_price is None and reference_price is not None:
                existing_market.reference_price = reference_price
            if existing_market.product_type == PRODUCT_TYPE_PERP:
                if Decimal(existing_market.max_leverage or ZERO) <= Decimal("1"):
                    existing_market.max_leverage = payload["max_leverage"]
                if Decimal(existing_market.default_leverage or ZERO) <= Decimal("1"):
                    existing_market.default_leverage = payload["default_leverage"]
                if Decimal(existing_market.maintenance_margin_rate or ZERO) <= ZERO:
                    existing_market.maintenance_margin_rate = payload["maintenance_margin_rate"]
                if int(existing_market.funding_interval_hours or 0) <= 0:
                    existing_market.funding_interval_hours = payload["funding_interval_hours"]
                if not existing_market.index_price_source:
                    existing_market.index_price_source = payload["index_price_source"]
                if not existing_market.mark_price_mode:
                    existing_market.mark_price_mode = payload["mark_price_mode"]
                if not existing_market.funding_rate_mode:
                    existing_market.funding_rate_mode = payload["funding_rate_mode"]
                if not existing_market.contract_trading_mode:
                    existing_market.contract_trading_mode = payload["contract_trading_mode"]
            # 兼容早期 BTCUSDT 默认精度 0.1/1 位小数，自动升级到 0.01/2 位小数。
            if (
                symbol == "BTCUSDT"
                and existing_market.price_tick == Decimal("0.1")
                and existing_market.price_precision == 1
                and existing_market.qty_step == Decimal("0.0001")
                and existing_market.qty_precision == 4
            ):
                existing_market.price_tick = Decimal("0.01")
                existing_market.price_precision = 2
            continue
        market = Market(**payload, reference_price=reference_price, is_active=True, visibility=MARKET_VISIBILITY_TEST)
        session.add(market)
        await session.flush()
        markets[market.symbol] = market
    return markets


async def _ensure_fee_profiles(session: AsyncSession, users: dict[str, User], markets: dict[str, Market]) -> None:
    existing = await session.execute(select(FeeProfile))
    keys = {(item.user_id, item.market_id) for item in existing.scalars()}
    for username, user in users.items():
        if user.role == ROLE_ADMIN:
            continue
        for market in markets.values():
            key = (user.id, market.id)
            if key in keys:
                continue
            if user.role == ROLE_BOT:
                maker = Decimal("0")
                taker = Decimal("0")
            else:
                maker = Decimal("0")
                taker = Decimal("0")
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=maker,
                    taker_fee_rate=taker,
                )
            )


async def _ensure_reset_templates(session: AsyncSession, users: dict[str, User]) -> None:
    existing = await session.execute(select(ResetTemplate))
    existing_map = {(item.name, item.user_id, item.asset): item for item in existing.scalars()}
    for username, assets in RESET_AMOUNTS.items():
        user = users.get(username)
        if user is None:
            continue
        for asset, amount in assets.items():
            key = ("default", user.id, asset)
            template = existing_map.get(key)
            if template is None:
                session.add(ResetTemplate(name="default", user_id=user.id, asset=asset, amount=amount))
            else:
                template.amount = amount


async def _ensure_initial_balances(session: AsyncSession, users: dict[str, User]) -> None:
    existing = await session.execute(select(Balance))
    existing_keys = {(item.user_id, item.asset) for item in existing.scalars()}
    now = datetime.now(tz=UTC)
    for username, assets in RESET_AMOUNTS.items():
        user = users.get(username)
        if user is None:
            continue
        for asset, amount in assets.items():
            if (user.id, asset) in existing_keys:
                continue
            # 全新库初始化为 0 的用户不需要生成零金额账务事件；
            # 直接创建零余额记录即可，避免被 SQLite 精度守卫拦截。
            if amount <= ZERO:
                session.add(Balance(user_id=user.id, asset=asset, available=ZERO, frozen=ZERO))
                existing_keys.add((user.id, asset))
                continue
            await AccountService().apply_change(
                session, user.id, asset,
                available_delta=amount, frozen_delta=ZERO,
                change_type="deposit_reset", amount=amount,
                note="bootstrap", created_at=now,
            )


async def _ensure_test_customer_accounts(
    session: AsyncSession,
    users: dict[str, User],
    test_markets: dict[str, Market],
) -> None:
    """Give seeded manual/admin test users a durable spot + perp account.

    Spot balances are seeded by ``_ensure_initial_balances``.  This helper only
    creates the missing Paper account run, contract wallet and contract
    settings; existing balances/positions remain untouched on bootstrap.
    """
    markets = list(test_markets.values())
    for user in users.values():
        if user.role not in {ROLE_ADMIN, ROLE_MANUAL}:
            continue
        await ensure_user_assets(
            session,
            user,
            markets,
            reason="test_account_bootstrap",
            preserve_balances=True,
        )


async def _unique_market_bot_username(session: AsyncSession, symbol: str, index: int) -> str:
    base = f"{symbol.lower()}_mm_{index}"[:48]
    candidate = base
    suffix = 1
    while await session.scalar(select(User.id).where(User.username == candidate)) is not None:
        suffix += 1
        candidate = f"{base}_{suffix}"[:64]
    return candidate


async def _unique_market_flow_username(session: AsyncSession, symbol: str, index: int) -> str:
    base = f"{symbol.lower()}_flow_{index}"[:48]
    candidate = base
    suffix = 1
    while await session.scalar(select(User.id).where(User.username == candidate)) is not None:
        suffix += 1
        candidate = f"{base}_{suffix}"[:64]
    return candidate


async def _ensure_market_bot_asset(
    session: AsyncSession,
    user: User,
    asset: str,
    amount: Decimal,
    *,
    note: str,
    now: datetime,
    comparison_step: Decimal,
) -> None:
    balance = await session.scalar(select(Balance).where(Balance.user_id == user.id, Balance.asset == asset))
    if balance is None:
        await AccountService().apply_change(
            session, user.id, asset,
            available_delta=amount, frozen_delta=ZERO,
            change_type="deposit_reset", amount=amount,
            note=note, created_at=now,
        )
    else:
        current_total = Decimal(balance.available or ZERO) + Decimal(balance.frozen or ZERO)
        # SQLite stores NUMERIC through REAL affinity. Tiny representation noise
        # must not manufacture a new financial event on every restart.
        if current_total + (comparison_step / Decimal("2")) < amount:
            top_up = amount - current_total
            await AccountService().apply_change(
                session, user.id, asset,
                available_delta=top_up, frozen_delta=ZERO,
                change_type="deposit_reset", amount=top_up,
                note=note, created_at=now,
            )
    template = await session.scalar(
        select(ResetTemplate).where(
            ResetTemplate.name == "default",
            ResetTemplate.user_id == user.id,
            ResetTemplate.asset == asset,
        )
    )
    if template is None:
        session.add(ResetTemplate(name="default", user_id=user.id, asset=asset, amount=amount))
    else:
        template.amount = amount


async def _ensure_market_bot_contract_account(
    session: AsyncSession,
    user: User,
    market: Market,
    margin_asset: str,
    wallet_balance: Decimal,
    *,
    note: str,
    now: datetime,
) -> None:
    asset = margin_asset.upper().strip() or "USDT"
    account = await session.scalar(
        select(ContractAccount).where(
            ContractAccount.user_id == user.id,
            ContractAccount.margin_asset == asset,
        )
    )
    if account is None:
        account = ContractAccount(
            user_id=user.id,
            margin_asset=asset,
            wallet_balance=wallet_balance,
            available_margin=wallet_balance,
            used_margin=ZERO,
            unrealized_pnl=ZERO,
            realized_pnl=ZERO,
            total_fees=ZERO,
            updated_at=now,
        )
        session.add(account)
        await session.flush()
        await add_contract_ledger_entry(
            session,
            account,
            change_type="account_init",
            amount=wallet_balance,
            before={
                "wallet": ZERO,
                "available": ZERO,
                "used_margin": ZERO,
                "unrealized_pnl": ZERO,
                "realized_pnl": ZERO,
                "total_fees": ZERO,
            },
            note=note,
            created_at=now,
            skip_if_unchanged=False,
        )
        await _ensure_contract_position_mode(session, user.id, market)
        return

    current_wallet = Decimal(account.wallet_balance or ZERO)
    if current_wallet + Decimal("0.005") < wallet_balance and wallet_balance > ZERO:
        before = snapshot_contract_account(account)
        top_up = wallet_balance - current_wallet
        account.wallet_balance = wallet_balance
        account.available_margin = Decimal(account.available_margin or ZERO) + top_up
        account.updated_at = now
        await add_contract_ledger_entry(
            session,
            account,
            change_type="admin_adjust",
            amount=top_up,
            before=before,
            note=note,
            created_at=now,
        )
    await _ensure_contract_position_mode(session, user.id, market)


async def _ensure_contract_position_mode(session: AsyncSession, user_id: int, market: Market) -> None:
    setting = await session.scalar(
        select(ContractUserSetting).where(
            ContractUserSetting.user_id == user_id,
            ContractUserSetting.market_id == market.id,
        )
    )
    if setting is None:
        session.add(
            ContractUserSetting(
                user_id=user_id,
                market_id=market.id,
                leverage=market.default_leverage,
                margin_mode="isolated",
                position_mode=POSITION_MODE_HEDGE,
            )
        )
        await session.flush()
        return
    setting.position_mode = POSITION_MODE_HEDGE


async def _ensure_market_bot_accounts(
    session: AsyncSession,
    markets: dict[str, Market],
    *,
    persist_financial: bool = True,
) -> None:
    now = datetime.now(tz=UTC)
    for market in markets.values():
        if market.product_type not in {PRODUCT_TYPE_SPOT, PRODUCT_TYPE_PERP}:
            continue
        reference_price = market.reference_price or _default_reference_price(market.symbol)
        if reference_price is None or Decimal(reference_price) <= ZERO:
            continue
        is_perp = market.product_type == PRODUCT_TYPE_PERP
        initial_quote_amount = DEFAULT_PERP_MARKET_BOT_QUOTE_AMOUNT if is_perp else DEFAULT_MARKET_BOT_QUOTE_AMOUNT
        initial_base_notional = DEFAULT_PERP_MARKET_BOT_BASE_NOTIONAL if is_perp else DEFAULT_MARKET_BOT_BASE_NOTIONAL
        existing_rows = await session.execute(
            select(MarketBotAccount).where(MarketBotAccount.market_id == market.id).order_by(MarketBotAccount.id.asc())
        )
        existing_bots = list(existing_rows.scalars())
        for bot in existing_bots:
            user = await session.scalar(select(User).where(User.id == bot.user_id))
            if user is None:
                continue
            if Decimal(bot.initial_quote_amount or ZERO) < initial_quote_amount:
                bot.initial_quote_amount = initial_quote_amount
            if is_perp:
                if persist_financial:
                    await _ensure_market_bot_contract_account(
                        session,
                        user,
                        market,
                        market.margin_asset or market.quote_asset,
                        Decimal(bot.initial_quote_amount),
                        note=f"bootstrap_contract_market_bot:{market.symbol}",
                        now=now,
                    )
                else:
                    await _ensure_contract_position_mode(session, user.id, market)
            else:
                target_base_amount = quantize_step(initial_base_notional / Decimal(reference_price), Decimal(market.qty_step))
                if Decimal(bot.initial_base_notional or ZERO) < initial_base_notional:
                    bot.initial_base_notional = initial_base_notional
                if Decimal(bot.initial_base_amount or ZERO) < target_base_amount:
                    bot.initial_base_amount = target_base_amount
                if persist_financial:
                    await _ensure_market_bot_asset(
                        session,
                        user,
                        market.quote_asset,
                        Decimal(bot.initial_quote_amount),
                        note=f"bootstrap_market_bot:{market.symbol}",
                        now=now,
                        comparison_step=Decimal("0.01"),
                    )
                    await _ensure_market_bot_asset(
                        session,
                        user,
                        market.base_asset,
                        Decimal(bot.initial_base_amount),
                        note=f"bootstrap_market_bot:{market.symbol}",
                        now=now,
                        comparison_step=Decimal(market.qty_step),
                    )
            profile = await session.scalar(
                select(FeeProfile).where(FeeProfile.user_id == user.id, FeeProfile.market_id == market.id)
            )
            if profile is None:
                session.add(
                    FeeProfile(
                        user_id=user.id,
                        market_id=market.id,
                        maker_fee_rate=DEFAULT_MARKET_BOT_MAKER_FEE,
                        taker_fee_rate=DEFAULT_MARKET_BOT_TAKER_FEE,
                    )
                )
        existing_maker_count = sum(1 for bot in existing_bots if bot.role == "maker")
        for index in range(existing_maker_count + 1, DEFAULT_MARKET_BOT_COUNT + 1):
            username = await _unique_market_bot_username(session, market.symbol, index)
            user = User(
                id=await next_uid_for_role(session, ROLE_BOT),
                username=username,
                role=ROLE_BOT,
                api_key=generate_api_key(username.replace("_", "-")[:18] or "bot"),
                api_secret_hash=generate_api_secret(),
                password_hash=hash_password(DEFAULT_MARKET_BOT_PASSWORD),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            if initial_base_notional > ZERO:
                base_amount = quantize_step(initial_base_notional / Decimal(reference_price), Decimal(market.qty_step))
            else:
                base_amount = ZERO
            if is_perp:
                if persist_financial:
                    await _ensure_market_bot_contract_account(
                        session,
                        user,
                        market,
                        market.margin_asset or market.quote_asset,
                        initial_quote_amount,
                        note=f"bootstrap_contract_market_bot:{market.symbol}",
                        now=now,
                    )
                else:
                    await _ensure_contract_position_mode(session, user.id, market)
            else:
                base_amount = quantize_step(initial_base_notional / Decimal(reference_price), Decimal(market.qty_step))
            if persist_financial and not is_perp:
                await _ensure_market_bot_asset(
                    session,
                    user,
                    market.quote_asset,
                    initial_quote_amount,
                    note=f"bootstrap_market_bot:{market.symbol}",
                    now=now,
                    comparison_step=Decimal("0.01"),
                )
                await _ensure_market_bot_asset(
                    session,
                    user,
                    market.base_asset,
                    base_amount,
                    note=f"bootstrap_market_bot:{market.symbol}",
                    now=now,
                    comparison_step=Decimal(market.qty_step),
                )
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=DEFAULT_MARKET_BOT_MAKER_FEE,
                    taker_fee_rate=DEFAULT_MARKET_BOT_TAKER_FEE,
                )
            )
            session.add(
                MarketBotAccount(
                    market_id=market.id,
                    user_id=user.id,
                    bot_label=f"{market.symbol}-MM-{index}",
                    role="maker",
                    strategy_role="maker",
                    initial_quote_amount=initial_quote_amount,
                    initial_base_amount=base_amount,
                    initial_base_notional=initial_base_notional,
                    reference_price=Decimal(reference_price),
                    is_enabled=True,
                )
            )
        flow_count = sum(1 for bot in existing_bots if bot.role == "flow")
        for index in range(flow_count + 1, DEFAULT_MARKET_FLOW_BOT_COUNT + 1):
            username = await _unique_market_flow_username(session, market.symbol, index)
            user = User(
                id=await next_uid_for_role(session, ROLE_BOT),
                username=username,
                role=ROLE_BOT,
                api_key=generate_api_key(username.replace("_", "-")[:18] or "flow"),
                api_secret_hash=generate_api_secret(),
                password_hash=hash_password(DEFAULT_MARKET_BOT_PASSWORD),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            base_amount = ZERO if is_perp else quantize_step(initial_base_notional / Decimal(reference_price), Decimal(market.qty_step))
            if is_perp:
                if persist_financial:
                    await _ensure_market_bot_contract_account(
                        session,
                        user,
                        market,
                        market.margin_asset or market.quote_asset,
                        initial_quote_amount,
                        note=f"bootstrap_contract_market_flow_bot:{market.symbol}",
                        now=now,
                    )
                else:
                    await _ensure_contract_position_mode(session, user.id, market)
            else:
                base_amount = ZERO if is_perp else quantize_step(initial_base_notional / Decimal(reference_price), Decimal(market.qty_step))
            if persist_financial and not is_perp:
                await _ensure_market_bot_asset(
                    session,
                    user,
                    market.quote_asset,
                    initial_quote_amount,
                    note=f"bootstrap_market_flow_bot:{market.symbol}",
                    now=now,
                    comparison_step=Decimal("0.01"),
                )
                await _ensure_market_bot_asset(
                    session,
                    user,
                    market.base_asset,
                    base_amount,
                    note=f"bootstrap_market_flow_bot:{market.symbol}",
                    now=now,
                    comparison_step=Decimal(market.qty_step),
                )
            session.add(
                FeeProfile(
                    user_id=user.id,
                    market_id=market.id,
                    maker_fee_rate=DEFAULT_MARKET_BOT_MAKER_FEE,
                    taker_fee_rate=DEFAULT_MARKET_BOT_TAKER_FEE,
                )
            )
            session.add(
                MarketBotAccount(
                    market_id=market.id,
                    user_id=user.id,
                    bot_label=f"{market.symbol}-FLOW-{index}",
                    role="flow",
                    strategy_role="flow",
                    initial_quote_amount=initial_quote_amount,
                    initial_base_amount=base_amount,
                    initial_base_notional=ZERO if is_perp else initial_base_notional,
                    reference_price=Decimal(reference_price),
                    is_enabled=True,
                )
            )


async def _symbols_missing_trade_history(session: AsyncSession) -> list[str]:
    missing: list[str] = []
    for symbol in MARKET_SEEDS:
        market = await session.scalar(select(Market).where(Market.symbol == symbol))
        if market is None:
            continue
        trade_count = await session.scalar(select(func.count()).select_from(Trade).where(Trade.market_id == market.id))
        if not trade_count:
            missing.append(symbol)
    return missing


async def _symbols_missing_open_orders(session: AsyncSession) -> list[str]:
    missing: list[str] = []
    for symbol in MARKET_SEEDS:
        market = await session.scalar(select(Market).where(Market.symbol == symbol))
        if market is None:
            continue
        open_count = await session.scalar(
            select(func.count())
            .select_from(Order)
            .where(
                Order.market_id == market.id,
                Order.status.in_([ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED]),
            )
        )
        if not open_count:
            missing.append(symbol)
    return missing


async def _seed_orderbook(
    session: AsyncSession,
    users: dict[str, User],
    service: OrderService,
    symbols: list[str] | None = None,
) -> None:
    placements = []
    seed_symbols = symbols or list(MARKET_SEEDS)
    for offset in range(1, 26):
        for symbol in seed_symbols:
            seed = MARKET_SEEDS[symbol]
            bid = seed["base_price"] - seed["book_gap"] * offset
            ask = seed["base_price"] + seed["book_gap"] * offset
            qty = seed["book_qty_base"] + seed["book_qty_step"] * offset
            placements.extend(
                [
                    (users["spot_mm_1"], symbol, SIDE_BUY, bid, qty),
                    (users["spot_mm_2"], symbol, SIDE_SELL, ask, qty),
                ]
            )

    for user, symbol, side, price, quantity in placements:
        request = OrderCreateRequest(
            symbol=symbol,
            side=side,
            type="limit",
            tif=TIF_GTC,
            price=price,
            quantity=quantity,
            client_order_id=f"seed-{symbol}-{side}-{price}",
        )
        await service.place_order(session, user, request)


async def _seed_trade_history(
    session: AsyncSession,
    users: dict[str, User],
    service: OrderService,
    symbols: list[str] | None = None,
) -> None:
    start = datetime.now(tz=UTC).replace(second=0, microsecond=0) - timedelta(minutes=180)
    seed_symbols = symbols or list(MARKET_SEEDS)
    for symbol in seed_symbols:
        seed = MARKET_SEEDS[symbol]
        for idx in range(360):
            ts = start + timedelta(seconds=30 * idx)
            wave = Decimal(str((idx % 24) - 12)) * seed["history_tick"]
            price = seed["base_price"] + wave
            quantity = seed["history_qty_base"] + (Decimal(idx % 7) * seed["history_qty_step"])
            # 交替买卖方，避免 mm1 始终买入耗尽 USDT
            taker_side = SIDE_BUY
            maker_side = SIDE_SELL
            if idx % 2 == 0:
                maker_user = users["spot_mm_2"]
                taker_user = users["spot_mm_1"]
            else:
                maker_user = users["spot_mm_1"]
                taker_user = users["spot_mm_2"]

            maker_request = OrderCreateRequest(
                symbol=symbol,
                side=maker_side,
                type="limit",
                tif=TIF_GTC,
                price=price,
                quantity=quantity,
                client_order_id=f"seed-maker-{symbol}-{idx}",
            )
            taker_request = OrderCreateRequest(
                symbol=symbol,
                side=taker_side,
                type="limit",
                tif=TIF_GTC,
                price=price,
                quantity=quantity,
                client_order_id=f"seed-taker-{symbol}-{idx}",
            )
            await service.place_order(session, maker_user, maker_request, now=ts)
            await service.place_order(session, taker_user, taker_request, now=ts + timedelta(milliseconds=10))
