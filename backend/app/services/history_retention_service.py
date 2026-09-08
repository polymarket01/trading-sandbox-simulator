from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.constants import ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED, ROLE_ADMIN, ROLE_BOT
from app.models.contract_ledger_entry import ContractLedgerEntry
from app.models.display_kline import DisplayKline
from app.models.kline import Kline
from app.models.ledger_entry import LedgerEntry
from app.models.market import Market
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User


LIVE_ORDER_STATUSES = (ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED)


def user_system_retention_condition(user_model) -> Any:
    username = func.lower(user_model.username)
    return or_(user_model.role == ROLE_ADMIN, username.like("contract_liq_%"))


def user_robot_retention_condition(user_model) -> Any:
    username = func.lower(user_model.username)
    return and_(
        ~user_system_retention_condition(user_model),
        or_(user_model.role == ROLE_BOT, username.like("%_mm_%"), username.like("%_flow_%")),
    )


def user_customer_retention_condition(user_model) -> Any:
    return and_(
        ~user_system_retention_condition(user_model),
        ~user_robot_retention_condition(user_model),
    )


@dataclass(frozen=True)
class HistoryRetentionConfig:
    order_keep_per_market: int
    trade_keep_per_market: int
    kline_keep_per_market_interval: int
    ledger_keep_per_user: int
    contract_ledger_keep_per_user: int
    batch_size: int
    financial_pruning_enabled: bool = True

    @classmethod
    def from_settings(cls) -> "HistoryRetentionConfig":
        return cls(
            order_keep_per_market=max(0, settings.history_retention_order_keep_per_market),
            trade_keep_per_market=max(0, settings.history_retention_trade_keep_per_market),
            kline_keep_per_market_interval=max(0, settings.history_retention_kline_keep_per_market_interval),
            ledger_keep_per_user=max(0, settings.history_retention_ledger_keep_per_user),
            contract_ledger_keep_per_user=max(0, settings.history_retention_contract_ledger_keep_per_user),
            batch_size=max(100, settings.history_retention_batch_size),
            financial_pruning_enabled=settings.history_retention_financial_pruning_enabled,
        )


class HistoryRetentionService:
    def __init__(self, config: HistoryRetentionConfig | None = None) -> None:
        self.config = config or HistoryRetentionConfig.from_settings()

    async def prune(self, session: AsyncSession, *, dry_run: bool = False) -> dict[str, Any]:
        if self.config.financial_pruning_enabled and not dry_run:
            raise RuntimeError(
                "financial pruning is fail-closed until robot checkpoint coverage, "
                "copy-database reconstruction, and a dedicated deletion migration are verified"
            )
        result: dict[str, Any] = {
            "dry_run": dry_run,
            "config": {
                "order_keep_per_market": self.config.order_keep_per_market,
                "trade_keep_per_market": self.config.trade_keep_per_market,
                "kline_keep_per_market_interval": self.config.kline_keep_per_market_interval,
                "ledger_keep_per_user": self.config.ledger_keep_per_user,
                "contract_ledger_keep_per_user": self.config.contract_ledger_keep_per_user,
                "batch_size": self.config.batch_size,
                "customer_records_preserved": True,
                "financial_pruning_enabled": self.config.financial_pruning_enabled,
            },
            "policy": {
                "customer_records": "preserve",
                "robot_records": "tail_prune_candidate",
                "system_records": "tail_prune_candidate",
                "kline_records": "tail_prune_by_market_interval",
            },
            "deleted": {
                "orders": 0,
                "trades": 0,
                "klines": 0,
                "display_klines": 0,
                "ledger_entries": 0,
                "contract_ledger_entries": 0,
            },
            "partitions": {
                "markets": 0,
                "kline_partitions": 0,
                "display_kline_partitions": 0,
                "ledger_users": 0,
                "contract_ledger_users": 0,
            },
        }
        market_ids = list((await session.execute(select(Market.id).order_by(Market.id.asc()))).scalars())
        result["partitions"]["markets"] = len(market_ids)

        for market_id in market_ids if self.config.financial_pruning_enabled else []:
            order_user = aliased(User)
            result["deleted"]["orders"] += await self._delete_tail_from_select(
                session,
                Order,
                Order.id,
                select(Order.id)
                .join(order_user, order_user.id == Order.user_id)
                .where(
                    Order.market_id == market_id,
                    Order.status.not_in(LIVE_ORDER_STATUSES),
                    ~user_customer_retention_condition(order_user),
                )
                .order_by(Order.created_at.desc(), Order.id.desc()),
                self.config.order_keep_per_market,
                dry_run=dry_run,
            )
            taker_user = aliased(User)
            maker_user = aliased(User)
            result["deleted"]["trades"] += await self._delete_tail_from_select(
                session,
                Trade,
                Trade.id,
                select(Trade.id)
                .join(taker_user, taker_user.id == Trade.taker_user_id)
                .join(maker_user, maker_user.id == Trade.maker_user_id)
                .where(
                    Trade.market_id == market_id,
                    ~or_(
                        user_customer_retention_condition(taker_user),
                        user_customer_retention_condition(maker_user),
                    ),
                )
                .order_by(Trade.executed_at.desc(), Trade.id.desc()),
                self.config.trade_keep_per_market,
                dry_run=dry_run,
            )

        kline_partitions = list(
            (
                await session.execute(
                    select(Kline.market_id, Kline.interval)
                    .distinct()
                    .order_by(Kline.market_id.asc(), Kline.interval.asc())
                )
            ).all()
        )
        result["partitions"]["kline_partitions"] = len(kline_partitions)
        for market_id, interval in kline_partitions:
            result["deleted"]["klines"] += await self._delete_tail(
                session,
                Kline,
                Kline.id,
                [Kline.market_id == market_id, Kline.interval == interval],
                [Kline.open_time.desc(), Kline.id.desc()],
                self.config.kline_keep_per_market_interval,
                dry_run=dry_run,
            )

        display_kline_partitions = list(
            (
                await session.execute(
                    select(DisplayKline.market_id, DisplayKline.interval)
                    .distinct()
                    .order_by(DisplayKline.market_id.asc(), DisplayKline.interval.asc())
                )
            ).all()
        )
        result["partitions"]["display_kline_partitions"] = len(display_kline_partitions)
        for market_id, interval in display_kline_partitions:
            result["deleted"]["display_klines"] += await self._delete_tail(
                session,
                DisplayKline,
                DisplayKline.id,
                [DisplayKline.market_id == market_id, DisplayKline.interval == interval],
                [DisplayKline.open_time.desc(), DisplayKline.id.desc()],
                self.config.kline_keep_per_market_interval,
                dry_run=dry_run,
            )

        ledger_user_ids = list(
            (
                await session.execute(
                    select(LedgerEntry.user_id)
                    .join(User, User.id == LedgerEntry.user_id)
                    .where(~user_customer_retention_condition(User))
                    .distinct()
                    .order_by(LedgerEntry.user_id.asc())
                )
            ).scalars()
        ) if self.config.financial_pruning_enabled else []
        result["partitions"]["ledger_users"] = len(ledger_user_ids)
        for user_id in ledger_user_ids:
            result["deleted"]["ledger_entries"] += await self._delete_tail(
                session,
                LedgerEntry,
                LedgerEntry.id,
                [LedgerEntry.user_id == user_id],
                [LedgerEntry.created_at.desc(), LedgerEntry.id.desc()],
                self.config.ledger_keep_per_user,
                dry_run=dry_run,
            )

        contract_ledger_user_ids = list(
            (
                await session.execute(
                    select(ContractLedgerEntry.user_id)
                    .join(User, User.id == ContractLedgerEntry.user_id)
                    .where(~user_customer_retention_condition(User))
                    .distinct()
                    .order_by(ContractLedgerEntry.user_id.asc())
                )
            ).scalars()
        ) if self.config.financial_pruning_enabled else []
        result["partitions"]["contract_ledger_users"] = len(contract_ledger_user_ids)
        for user_id in contract_ledger_user_ids:
            result["deleted"]["contract_ledger_entries"] += await self._delete_tail(
                session,
                ContractLedgerEntry,
                ContractLedgerEntry.id,
                [ContractLedgerEntry.user_id == user_id],
                [ContractLedgerEntry.created_at.desc(), ContractLedgerEntry.id.desc()],
                self.config.contract_ledger_keep_per_user,
                dry_run=dry_run,
            )

        if not dry_run:
            await session.commit()
        return result

    async def _delete_tail(
        self,
        session: AsyncSession,
        model,
        id_column,
        predicates: list[Any],
        order_by: list[Any],
        keep: int,
        *,
        dry_run: bool,
    ) -> int:
        return await self._delete_tail_from_select(
            session,
            model,
            id_column,
            select(id_column).where(*predicates).order_by(*order_by),
            keep,
            dry_run=dry_run,
        )

    async def _delete_tail_from_select(
        self,
        session: AsyncSession,
        model,
        id_column,
        candidate_ids,
        keep: int,
        *,
        dry_run: bool,
    ) -> int:
        if dry_run:
            total_rows = int(
                await session.scalar(select(func.count()).select_from(candidate_ids.subquery())) or 0
            )
            return max(total_rows - keep, 0)

        total = 0
        while True:
            ids_to_prune = candidate_ids.offset(keep).limit(self.config.batch_size)
            delete_result = await session.execute(delete(model).where(id_column.in_(ids_to_prune)))
            count = int(delete_result.rowcount or 0)
            total += count
            if count:
                await session.commit()
            if count < self.config.batch_size:
                break
        return total


def history_retention_auto_enabled() -> bool:
    if not settings.history_retention_enabled:
        return False
    if settings.history_retention_sqlite_only and not settings.database_url.startswith("sqlite"):
        return False
    return True
