from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    POSITION_SIDE_FLAT,
    POSITION_SIDE_LONG,
    POSITION_SIDE_SHORT,
    ZERO,
)
from app.models.balance import Balance
from app.models.contract_account import ContractAccount
from app.models.contract_position import ContractPosition


@dataclass(slots=True)
class SpotBalanceState:
    available: Decimal = ZERO
    frozen: Decimal = ZERO


@dataclass(slots=True)
class ContractAccountState:
    wallet_balance: Decimal = ZERO
    used_margin: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    total_fees: Decimal = ZERO

    @property
    def available_margin(self) -> Decimal:
        return Decimal(self.wallet_balance) + Decimal(self.unrealized_pnl) - Decimal(self.used_margin)


@dataclass(slots=True)
class PositionState:
    user_id: int
    market_id: int
    side: str = POSITION_SIDE_FLAT
    quantity: Decimal = ZERO
    entry_price: Decimal = ZERO
    isolated_margin: Decimal = ZERO
    maintenance_margin: Decimal = ZERO
    leverage: Decimal = Decimal("1")
    mark_price: Decimal | None = None
    realized_pnl: Decimal = ZERO

    @property
    def notional(self) -> Decimal:
        return Decimal(self.quantity) * Decimal(self.entry_price or ZERO)

    def unrealized(self, mark_price: Decimal | None = None) -> Decimal:
        mark = mark_price if mark_price is not None else self.mark_price
        if mark is None or Decimal(self.quantity) == 0:
            return ZERO
        if self.side == POSITION_SIDE_LONG:
            return (Decimal(mark) - Decimal(self.entry_price)) * Decimal(self.quantity)
        if self.side == POSITION_SIDE_SHORT:
            return (Decimal(self.entry_price) - Decimal(mark)) * Decimal(self.quantity)
        return ZERO


class Clearinghouse:
    """In-memory account state used by the hot order path.

    Customer and every actual matched financial fact remain durable in SQLite.
    Synthetic FLOW never enters this mirror.  In sandbox ``memory`` mode, the
    mirror is still the live planning source for robot accounts and positions;
    it is rebuilt authoritatively from the durable baseline at startup and
    mutated atomically in the matching hot path.
    """

    def __init__(self) -> None:
        self._spot: dict[tuple[int, str], SpotBalanceState] = {}
        self._contract: dict[tuple[int, str], ContractAccountState] = {}
        self._positions: dict[tuple[int, int], PositionState] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_lock = asyncio.Lock()

    @property
    def global_lock(self) -> asyncio.Lock:
        return self._global_lock

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------
    def _spot_key(self, user_id: int, asset: str) -> str:
        return f"spot:{user_id}:{asset.upper()}"

    def _contract_key(self, user_id: int, asset: str) -> str:
        return f"contract:{user_id}:{asset.upper()}"

    def _position_key(self, user_id: int, market_id: int) -> str:
        return f"position:{user_id}:{market_id}"

    async def acquire(self, *keys: str) -> None:
        """Acquire locks in stable sorted order to avoid deadlock."""
        locks = [self._locks[key] for key in sorted(keys)]
        for lock in locks:
            await lock.acquire()

    def release(self, *keys: str) -> None:
        for key in sorted(keys, reverse=True):
            self._locks[key].release()

    # ------------------------------------------------------------------
    # Loading / snapshots
    # ------------------------------------------------------------------
    async def load_from_db(self, session: AsyncSession) -> dict:
        # This is an authoritative rebuild, not an incremental merge.  A
        # failed fast contract settlement may have created an in-memory
        # position before a later leg rejected.  Keeping keys that no longer
        # exist in SQLite turns that partial mutation into a ghost position and
        # can make the robot submit an invalid CLOSE hours later.
        self._spot.clear()
        self._contract.clear()
        self._positions.clear()
        spot_rows = (await session.execute(select(Balance))).scalars()
        for row in spot_rows:
            self._spot[(int(row.user_id), str(row.asset).upper())] = SpotBalanceState(
                available=Decimal(row.available or ZERO),
                frozen=Decimal(row.frozen or ZERO),
            )
        contract_rows = (await session.execute(select(ContractAccount))).scalars()
        for row in contract_rows:
            self._contract[(int(row.user_id), str(row.margin_asset).upper())] = ContractAccountState(
                wallet_balance=Decimal(row.wallet_balance or ZERO),
                used_margin=Decimal(row.used_margin or ZERO),
                unrealized_pnl=Decimal(row.unrealized_pnl or ZERO),
                realized_pnl=Decimal(row.realized_pnl or ZERO),
                total_fees=Decimal(row.total_fees or ZERO),
            )
        position_rows = (await session.execute(select(ContractPosition))).scalars()
        for row in position_rows:
            self._positions[(int(row.user_id), int(row.market_id))] = PositionState(
                user_id=int(row.user_id),
                market_id=int(row.market_id),
                side=str(row.side or POSITION_SIDE_FLAT),
                quantity=Decimal(row.quantity or ZERO),
                entry_price=Decimal(row.entry_price or ZERO),
                isolated_margin=Decimal(row.isolated_margin or ZERO),
                maintenance_margin=Decimal(row.maintenance_margin or ZERO),
                leverage=Decimal(row.leverage or 1),
                realized_pnl=Decimal(row.realized_pnl or ZERO),
            )
        return {
            "spot_accounts": len(self._spot),
            "contract_accounts": len(self._contract),
            "positions": len(self._positions),
        }

    def spot_snapshot(self, user_id: int, asset: str) -> SpotBalanceState | None:
        return self._spot.get((int(user_id), str(asset).upper()))

    def set_spot_balance(
        self,
        user_id: int,
        asset: str,
        *,
        available: Decimal,
        frozen: Decimal,
    ) -> None:
        """Publish one just-committed balance without rebuilding all accounts.

        Paper admin actions can provision a new asset while the process is
        live.  Rebuilding the complete mirror at that point could overwrite a
        concurrent fast-path balance, so the caller updates only the newly
        provisioned key under ``global_lock``.
        """

        self._spot[(int(user_id), str(asset).upper())] = SpotBalanceState(
            available=Decimal(available or ZERO),
            frozen=Decimal(frozen or ZERO),
        )

    def spot_snapshots(self, user_id: int) -> list[tuple[str, SpotBalanceState]]:
        return [
            (asset, state)
            for (owner_id, asset), state in sorted(self._spot.items(), key=lambda item: item[0][1])
            if owner_id == int(user_id)
        ]

    def contract_snapshot(self, user_id: int, asset: str) -> ContractAccountState | None:
        return self._contract.get((int(user_id), str(asset).upper()))

    def ensure_contract_account(self, user_id: int, asset: str) -> ContractAccountState:
        key = (int(user_id), str(asset).upper())
        return self._contract.setdefault(key, ContractAccountState())

    def position_snapshot(self, user_id: int, market_id: int) -> PositionState | None:
        return self._positions.get((int(user_id), int(market_id)))

    async def refresh_contract_market(self, session: AsyncSession, market_id: int) -> int:
        """Refresh in-memory contract accounts/positions for one market from DB.

        Used by the legacy contract path before matching so the per-fill maker
        margin guard sees near-current account state without DB reads per fill.
        """
        rows = await session.execute(
            select(ContractPosition).where(ContractPosition.market_id == int(market_id))
        )
        positions = list(rows.scalars())
        # Replace this market's position set.  Merely overwriting returned rows
        # leaves a position that was rolled back/deleted in the database alive
        # forever in the hot-path mirror.
        for key in [key for key in self._positions if key[1] == int(market_id)]:
            self._positions.pop(key, None)
        account_rows = await session.execute(
            select(ContractAccount)
        )
        for row in account_rows.scalars():
            self._contract[(int(row.user_id), str(row.margin_asset).upper())] = ContractAccountState(
                wallet_balance=Decimal(row.wallet_balance or ZERO),
                used_margin=Decimal(row.used_margin or ZERO),
                unrealized_pnl=Decimal(row.unrealized_pnl or ZERO),
                realized_pnl=Decimal(row.realized_pnl or ZERO),
                total_fees=Decimal(row.total_fees or ZERO),
            )
        for row in positions:
            self._positions[(int(row.user_id), int(row.market_id))] = PositionState(
                user_id=int(row.user_id),
                market_id=int(row.market_id),
                side=str(row.side or POSITION_SIDE_FLAT),
                quantity=Decimal(row.quantity or ZERO),
                entry_price=Decimal(row.entry_price or ZERO),
                isolated_margin=Decimal(row.isolated_margin or ZERO),
                maintenance_margin=Decimal(row.maintenance_margin or ZERO),
                leverage=Decimal(row.leverage or 1),
                realized_pnl=Decimal(row.realized_pnl or ZERO),
            )
        return len(positions)

    # ------------------------------------------------------------------
    # Spot mutations (hot path; caller already holds the spot key lock)
    # ------------------------------------------------------------------
    def spot_available(self, user_id: int, asset: str) -> Decimal:
        state = self._spot.get((int(user_id), str(asset).upper()))
        return Decimal(state.available) if state is not None else ZERO

    def reserve_spot(self, user_id: int, asset: str, amount: Decimal) -> None:
        key = (int(user_id), str(asset).upper())
        state = self._spot.setdefault(key, SpotBalanceState())
        if Decimal(amount) < 0:
            raise ValueError("reserve amount cannot be negative")
        if Decimal(state.available) < Decimal(amount) - Decimal("1e-12"):
            raise ValueError(f"insufficient available balance for asset {asset}")
        state.available = Decimal(state.available) - Decimal(amount)
        state.frozen = Decimal(state.frozen) + Decimal(amount)

    def release_spot(self, user_id: int, asset: str, amount: Decimal) -> None:
        key = (int(user_id), str(asset).upper())
        state = self._spot.setdefault(key, SpotBalanceState())
        if Decimal(amount) < 0:
            raise ValueError("release amount cannot be negative")
        if Decimal(state.frozen) < Decimal(amount) - Decimal("1e-12"):
            raise ValueError(f"insufficient frozen balance for asset {asset}")
        state.frozen = Decimal(state.frozen) - Decimal(amount)
        state.available = Decimal(state.available) + Decimal(amount)

    def apply_spot_delta(
        self,
        user_id: int,
        asset: str,
        *,
        available_delta: Decimal,
        frozen_delta: Decimal,
    ) -> SpotBalanceState:
        key = (int(user_id), str(asset).upper())
        state = self._spot.setdefault(key, SpotBalanceState())
        state.available = Decimal(state.available) + Decimal(available_delta)
        state.frozen = Decimal(state.frozen) + Decimal(frozen_delta)
        if Decimal(state.available) < 0 or Decimal(state.frozen) < 0:
            raise ValueError(f"balance would become negative for user={user_id} asset={asset}")
        return state

    # ------------------------------------------------------------------
    # Contract mutations (hot path; caller already holds locks)
    # ------------------------------------------------------------------
    def contract_available(self, user_id: int, asset: str) -> Decimal:
        state = self._contract.get((int(user_id), str(asset).upper()))
        return Decimal(state.available_margin) if state is not None else ZERO

    def reserve_contract_margin(self, user_id: int, asset: str, amount: Decimal) -> None:
        key = (int(user_id), str(asset).upper())
        state = self._contract.setdefault(key, ContractAccountState())
        reserve = Decimal(amount)
        if reserve < ZERO:
            raise ValueError("reserve amount cannot be negative")
        if Decimal(state.available_margin) < reserve - Decimal("1e-12"):
            raise ValueError(f"insufficient available margin for asset {asset}")
        state.used_margin = Decimal(state.used_margin) + reserve

    def release_contract_margin(self, user_id: int, asset: str, amount: Decimal) -> None:
        key = (int(user_id), str(asset).upper())
        state = self._contract.setdefault(key, ContractAccountState())
        state.used_margin = max(ZERO, Decimal(state.used_margin) - Decimal(amount))

    def apply_contract_delta(
        self,
        user_id: int,
        asset: str,
        *,
        wallet_delta: Decimal = ZERO,
        used_margin_delta: Decimal = ZERO,
        unrealized_pnl_delta: Decimal = ZERO,
        realized_pnl_delta: Decimal = ZERO,
        fees_delta: Decimal = ZERO,
    ) -> ContractAccountState:
        key = (int(user_id), str(asset).upper())
        state = self._contract.setdefault(key, ContractAccountState())
        state.wallet_balance = Decimal(state.wallet_balance) + Decimal(wallet_delta)
        state.used_margin = Decimal(state.used_margin) + Decimal(used_margin_delta)
        state.unrealized_pnl = Decimal(state.unrealized_pnl) + Decimal(unrealized_pnl_delta)
        state.realized_pnl = Decimal(state.realized_pnl) + Decimal(realized_pnl_delta)
        state.total_fees = Decimal(state.total_fees) + Decimal(fees_delta)
        return state

    def upsert_position(self, position: PositionState) -> None:
        self._positions[(int(position.user_id), int(position.market_id))] = position

    def mark_to_market(self, market_id: int, mark_price: Decimal) -> None:
        for position in self._positions.values():
            if position.market_id != int(market_id):
                continue
            old_unrealized = position.unrealized()
            position.mark_price = Decimal(mark_price)
            new_unrealized = position.unrealized()
            delta = new_unrealized - old_unrealized
            if delta == 0:
                continue
            account = self._contract.get((int(position.user_id), "USDT"))
            if account is None:
                continue
            account.unrealized_pnl = Decimal(account.unrealized_pnl) + delta

    def maker_margin_ok(self, user_id: int, market_id: int, maintenance_margin: Decimal) -> bool:
        """Solvency check used per fill for resting makers (Hyperliquid-style)."""
        account = self._contract.get((int(user_id), "USDT"))
        if account is None:
            return False
        equity = Decimal(account.wallet_balance) + Decimal(account.unrealized_pnl)
        return equity + Decimal("-1e-8") >= Decimal(maintenance_margin)

    def metrics_snapshot(self) -> dict:
        return {
            "spot_accounts": len(self._spot),
            "contract_accounts": len(self._contract),
            "positions": len(self._positions),
            "locks": len(self._locks),
        }
