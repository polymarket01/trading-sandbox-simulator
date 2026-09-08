from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.decimal_utils import quantize_step
from app.core.constants import (
    POSITION_SIDE_FLAT,
    ROLE_ADMIN,
    ROLE_BOT,
    ROLE_MANUAL,
    ROLE_USER,
    ZERO,
)
from app.core.security import generate_api_key, generate_api_secret, hash_password, hash_session_token
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_position import ContractPosition
from app.models.contract_user_setting import ContractUserSetting
from app.models.market import Market
from app.models.order import Order
from app.models.paper_exchange import (
    PaperAccountRun,
    PaperGlobalRun,
    PaperResetRecord,
    PaperSession,
    PaperSystemSetting,
)
from app.models.user import User
from app.services.account_service import AccountService
from app.services.contract_ledger import add_contract_ledger_entry, snapshot_contract_account


PAPER_USER_ROLES = {ROLE_USER, ROLE_MANUAL, ROLE_BOT, ROLE_ADMIN}


def normalize_username(username: str) -> str:
    return str(username or "").strip().lower()


def paper_maker_inventory_target(
    reference_price: Decimal,
    price_tick: Decimal,
    qty_step: Decimal,
    depth_quote_per_side: Decimal = Decimal("5000000"),
) -> Decimal:
    """Return enough base inventory for the default synthetic ask ladder.

    The old fixed one-million-unit seed works for BTC/ETH but leaves cheap
    dynamically listed spot markets without an ask side.  Size the inventory
    from the configured quote depth and keep a modest price/rounding buffer.
    """

    reference = max(Decimal(reference_price), Decimal(price_tick), Decimal("0.00000001"))
    target = max(Decimal("1000000"), depth_quote_per_side / reference * Decimal("1.25"))
    step = max(Decimal(qty_step), Decimal("0.00000001"))
    return max(step, quantize_step(target, step))


def new_account_run_id(user_id: int, epoch: int) -> str:
    return f"paper-u{int(user_id)}-e{int(epoch)}-{uuid.uuid4().hex[:12]}"


def serialize_user(user: User) -> dict:
    return {
        "user_id": int(user.id),
        "username": user.username,
        "role": user.role,
        "is_active": bool(user.is_active),
        "account_epoch": int(user.account_epoch or 1),
        "account_run_id": user.current_account_run_id,
        # Paper users authenticate with the HttpOnly cookie.  Existing bot and
        # legacy accounts retain the old API-key fields for compatibility.
        "api_key": user.api_key if user.role != ROLE_USER else None,
        "api_secret": user.api_secret_hash if user.role != ROLE_USER else None,
    }


async def ensure_global_run(session: AsyncSession) -> PaperGlobalRun:
    row = await session.scalar(select(PaperGlobalRun).where(PaperGlobalRun.id == 1))
    if row is None:
        row = PaperGlobalRun(id=1, run_id="paper-global-1", global_epoch=1, status="active")
        session.add(row)
        await session.flush()
    return row


async def ensure_account_run(
    session: AsyncSession,
    user: User,
    *,
    reason: str = "account_init",
    actor_user_id: int | None = None,
    reset: bool = False,
) -> PaperAccountRun:
    current_id = str(user.current_account_run_id or "").strip()
    if current_id and not reset:
        existing = await session.scalar(
            select(PaperAccountRun).where(
                PaperAccountRun.user_id == user.id,
                PaperAccountRun.run_id == current_id,
                PaperAccountRun.status == "active",
            )
        )
        if existing is not None:
            return existing
    old = None
    if current_id:
        old = await session.scalar(
            select(PaperAccountRun).where(
                PaperAccountRun.user_id == user.id,
                PaperAccountRun.run_id == current_id,
            )
        )
        if old is not None and old.status == "active":
            old.status = "ended"
            old.ended_at = datetime.now(tz=UTC)
    epoch = max(int(user.account_epoch or 0), 1) + (1 if reset else 0)
    user.account_epoch = epoch
    run_id = new_account_run_id(int(user.id), epoch)
    run = PaperAccountRun(
        run_id=run_id,
        user_id=user.id,
        account_epoch=epoch,
        status="active",
        scope="user",
        actor_user_id=actor_user_id,
        reason=reason,
    )
    user.current_account_run_id = run_id
    session.add(run)
    await session.flush()
    return run


async def ensure_user_assets(
    session: AsyncSession,
    user: User,
    markets: list[Market],
    *,
    reset: bool = False,
    reason: str = "account_init",
    actor_user_id: int | None = None,
    preserve_balances: bool = False,
) -> PaperAccountRun:
    """Create/align the spot + contract account for one user.

    ``preserve_balances=True`` keeps every existing balance unchanged (only
    missing rows are created).  It is used for system accounts like the Paper
    market maker whose spot inventory is managed by the listing pipeline.
    Without ``reset=True``, existing balances and contract state are also
    preserved; only a newly created account receives the default funding.
    ``reset=True`` is the explicit user/admin recovery operation and restores
    the configured spot and perp starting amounts.
    """
    run = await ensure_account_run(
        session,
        user,
        reason=reason,
        actor_user_id=actor_user_id,
        reset=reset,
    )
    defaults_row = await session.scalar(
        select(PaperSystemSetting).where(PaperSystemSetting.key == "paper_defaults")
    )
    defaults = defaults_row.value_json if defaults_row is not None and isinstance(defaults_row.value_json, dict) else {}
    spot_target = Decimal(str(defaults.get("spot_initial_usdt", settings.paper_exchange_default_spot_usdt)))
    perp_target = Decimal(str(defaults.get("perp_initial_usdt", settings.paper_exchange_default_perp_usdt)))
    configured_default_leverage = Decimal(str(defaults.get("default_leverage", "0")))
    assets = {"USDT"}
    for market in markets:
        assets.add(str(market.base_asset).upper())
        assets.add(str(market.quote_asset).upper())
        if market.margin_asset:
            assets.add(str(market.margin_asset).upper())
    account_service = AccountService()
    existing_rows = await session.execute(select(Balance).where(Balance.user_id == user.id))
    existing = {str(row.asset).upper(): row for row in existing_rows.scalars()}
    for asset in sorted(assets):
        balance = existing.get(asset)
        created = balance is None
        if balance is None:
            balance = Balance(
                user_id=user.id,
                asset=asset,
                available=ZERO,
                frozen=ZERO,
                account_run_id=run.run_id,
            )
            session.add(balance)
            await session.flush()
        balance.account_run_id = run.run_id
        if preserve_balances:
            continue
        if not reset and not created:
            # ensure_user_assets is also called by bootstrap/listing paths.
            # Those paths must never turn a normal account-init check into a
            # silent balance reset; only registration/new rows and explicit
            # reset requests may apply the default funding.
            continue
        # Spot and perp are separate ledgers.  The spot balance uses the spot
        # default; the contract wallet below uses the perp default.
        target = spot_target if asset == "USDT" else ZERO
        current_total = Decimal(balance.available or ZERO) + Decimal(balance.frozen or ZERO)
        if current_total != target or Decimal(balance.frozen or ZERO) != ZERO:
            await account_service.apply_change(
                session,
                user.id,
                asset,
                available_delta=target - Decimal(balance.available or ZERO),
                frozen_delta=-Decimal(balance.frozen or ZERO),
                change_type="paper_reset" if reset else "paper_initial_balance",
                amount=target - current_total,
                note=reason,
            )
            balance = await session.scalar(
                select(Balance).where(Balance.user_id == user.id, Balance.asset == asset)
            )
            if balance is not None:
                balance.account_run_id = run.run_id

    # Contract account and position are current-state records. Their previous
    # values remain in the contract ledger/trade history and the new run id
    # makes post-reset queries unambiguous.
    contract_markets = [market for market in markets if market.product_type == "PERP"]
    if contract_markets:
        account = await session.scalar(
            select(ContractAccount).where(
                ContractAccount.user_id == user.id,
                ContractAccount.margin_asset == "USDT",
            )
        )
        if account is None:
            account = ContractAccount(
                user_id=user.id,
                margin_asset="USDT",
                wallet_balance=perp_target,
                available_margin=perp_target,
                used_margin=ZERO,
                unrealized_pnl=ZERO,
                realized_pnl=ZERO,
                total_fees=ZERO,
                account_run_id=run.run_id,
            )
            session.add(account)
            await session.flush()
            await add_contract_ledger_entry(
                session,
                account,
                change_type="paper_initial_balance",
                amount=perp_target,
                before={"wallet": ZERO, "available": ZERO, "used_margin": ZERO, "unrealized_pnl": ZERO, "realized_pnl": ZERO, "total_fees": ZERO},
                note=reason,
                skip_if_unchanged=False,
            )
        elif reset:
            before = snapshot_contract_account(account)
            account.wallet_balance = perp_target
            account.used_margin = ZERO
            account.unrealized_pnl = ZERO
            account.realized_pnl = ZERO
            account.total_fees = ZERO
            account.available_margin = perp_target
            account.account_run_id = run.run_id
            await add_contract_ledger_entry(
                session,
                account,
                change_type="paper_reset",
                amount=perp_target - Decimal(before["wallet"]),
                before=before,
                note=reason,
                skip_if_unchanged=False,
            )
        account.account_run_id = run.run_id
        for market in contract_markets:
            positions = await session.execute(
                select(ContractPosition).where(
                    ContractPosition.user_id == user.id,
                    ContractPosition.market_id == market.id,
                )
            )
            position_rows = list(positions.scalars())
            if reset and position_rows:
                # A reset must leave one reusable flat row, but flattening
                # several hedge/one-way rows in one UPDATE would violate the
                # unique (user, market, side) constraint when a flat
                # placeholder already exists.  Delete only the redundant
                # position rows; trades and contract ledger history remain.
                keeper = next(
                    (item for item in position_rows if item.side == POSITION_SIDE_FLAT),
                    position_rows[0],
                )
                for position in position_rows:
                    if position is not keeper:
                        await session.delete(position)
            else:
                keeper = None
            for position in position_rows:
                if keeper is not None and position is not keeper:
                    continue
                position.account_run_id = run.run_id
                if reset:
                    position.side = POSITION_SIDE_FLAT
                    position.quantity = ZERO
                    position.entry_price = ZERO
                    position.liquidation_price = ZERO
                    position.isolated_margin = ZERO
                    position.maintenance_margin = ZERO
                    position.unrealized_pnl = ZERO
                    position.realized_pnl = ZERO
            setting = await session.scalar(
                select(ContractUserSetting).where(
                    ContractUserSetting.user_id == user.id,
                    ContractUserSetting.market_id == market.id,
                )
            )
            if setting is None:
                default_leverage = Decimal(market.default_leverage)
                if configured_default_leverage > ZERO:
                    default_leverage = min(default_leverage, configured_default_leverage)
                session.add(
                    ContractUserSetting(
                        user_id=user.id,
                        market_id=market.id,
                        leverage=default_leverage,
                        margin_mode="isolated",
                        position_mode="one_way",
                    )
                )
    await session.flush()
    return run


async def register_user(session: AsyncSession, username: str, password: str, markets: list[Market]) -> User:
    normalized = normalize_username(username)
    existing = await session.scalar(select(User).where(User.username_normalized == normalized))
    if existing is None:
        existing = await session.scalar(select(User).where(User.username == username.strip()))
    if existing is not None:
        raise ValueError("用户名已存在")
    user = User(
        username=username.strip(),
        username_normalized=normalized,
        role=ROLE_USER,
        api_key=None,
        api_secret_hash=None,
        password_hash=hash_password(password),
        is_active=True,
        account_epoch=0,
    )
    session.add(user)
    await session.flush()
    await ensure_user_assets(session, user, markets, reason="registration")
    await session.commit()
    return user


async def publish_new_user_runtime_state(session: AsyncSession, user: User, runtime) -> None:
    """Publish a newly registered account without rebuilding live maker state.

    The Paper API uses the fast clearinghouse path for customer orders.  A
    user registered after process startup therefore needs its committed
    balances published into the in-memory mirror before the first order.  Only
    missing keys are added; an existing key is never overwritten with a stale
    database snapshot while a write-behind order is in flight.
    """
    clearinghouse = runtime.clearinghouse
    async with clearinghouse.global_lock:
        balance_rows = await session.execute(select(Balance).where(Balance.user_id == int(user.id)))
        for row in balance_rows.scalars():
            if clearinghouse.spot_snapshot(int(user.id), row.asset) is None:
                clearinghouse.set_spot_balance(
                    int(user.id),
                    row.asset,
                    available=Decimal(row.available or ZERO),
                    frozen=Decimal(row.frozen or ZERO),
                )
        account_rows = await session.execute(
            select(ContractAccount).where(ContractAccount.user_id == int(user.id))
        )
        for row in account_rows.scalars():
            if clearinghouse.contract_snapshot(int(user.id), row.margin_asset) is not None:
                continue
            state = clearinghouse.ensure_contract_account(int(user.id), row.margin_asset)
            state.wallet_balance = Decimal(row.wallet_balance or ZERO)
            state.used_margin = Decimal(row.used_margin or ZERO)
            state.unrealized_pnl = Decimal(row.unrealized_pnl or ZERO)
            state.realized_pnl = Decimal(row.realized_pnl or ZERO)
            state.total_fees = Decimal(row.total_fees or ZERO)


async def create_session(
    session: AsyncSession,
    user: User,
    *,
    user_agent: str | None = None,
    remember: bool = True,
) -> str:
    raw = secrets.token_urlsafe(48)
    now = datetime.now(tz=UTC)
    ttl = int(settings.paper_exchange_session_ttl_seconds if remember else min(settings.paper_exchange_session_ttl_seconds, 86400))
    session.add(
        PaperSession(
            token_hash=hash_session_token(raw),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=max(300, ttl)),
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = now
    await session.commit()
    return raw


async def delete_session(session: AsyncSession, raw_token: str | None) -> None:
    if raw_token:
        await session.execute(delete(PaperSession).where(PaperSession.token_hash == hash_session_token(raw_token)))
        await session.commit()


async def reset_user_account(
    session: AsyncSession,
    user: User,
    *,
    markets: list[Market],
    runtime,
    order_service,
    contract_service,
    actor_user_id: int | None = None,
    reason: str = "user_reset",
) -> dict:
    async with runtime.clearinghouse.global_lock:
        old_run_id = user.current_account_run_id
        old_epoch = int(user.account_epoch or 1)
        for market in markets:
            try:
                if market.product_type == "SPOT":
                    await order_service.cancel_all(session, user, market.symbol, target_user_id=user.id)
                else:
                    rows = await session.execute(
                        select(Order.order_id).where(
                            Order.user_id == user.id,
                            Order.market_id == market.id,
                            Order.status.in_(["new", "partially_filled"]),
                        )
                    )
                    for (order_id,) in rows.all():
                        try:
                            await contract_service.cancel_order(session, user, order_id)
                        except Exception:
                            await session.rollback()
            except Exception:
                await session.rollback()
        run = await ensure_user_assets(
            session,
            user,
            markets,
            reset=True,
            reason=reason,
            actor_user_id=actor_user_id,
        )
        session.add(
            PaperResetRecord(
                scope="user",
                user_id=user.id,
                actor_user_id=actor_user_id,
                old_run_id=old_run_id,
                new_run_id=run.run_id,
                old_epoch=old_epoch,
                new_epoch=run.account_epoch,
                reason=reason,
            )
        )
        await session.commit()
        await runtime.clearinghouse.load_from_db(session)
        return {
            "user_id": user.id,
            "old_run_id": old_run_id,
            "account_run_id": run.run_id,
            "account_epoch": run.account_epoch,
            "reset": True,
        }


async def reset_all_accounts(
    session: AsyncSession,
    *,
    markets: list[Market],
    users: list[User],
    runtime,
    order_service,
    contract_service,
    actor_user_id: int | None,
    reason: str = "global_reset",
) -> dict:
    results = []
    for user in users:
        if user.role == ROLE_ADMIN:
            continue
        result = await reset_user_account(
            session,
            user,
            markets=markets,
            runtime=runtime,
            order_service=order_service,
            contract_service=contract_service,
            actor_user_id=actor_user_id,
            reason=reason,
        )
        results.append(result)
    global_run = await ensure_global_run(session)
    next_epoch = int(global_run.global_epoch) + 1
    next_run_id = f"paper-global-{next_epoch}-{uuid.uuid4().hex[:8]}"
    global_run.run_id = next_run_id
    global_run.global_epoch = next_epoch
    global_run.status = "active"
    # Clear any in-memory quote state left by the system maker; the liquidity
    # service will publish a fresh generation after the reset.
    for market in markets:
        runtime.engine.clear_market(market.symbol)
        runtime.published_orderbooks.pop(market.symbol.upper(), None)
    runtime._fast_orders.clear()
    runtime._fast_contract_orders.clear()
    await session.commit()
    await runtime.clearinghouse.load_from_db(session)
    return {"reset": True, "scope": "global", "users": results, "global_run_id": global_run.run_id}
