from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import POSITION_SIDE_LONG, POSITION_SIDE_SHORT, PRODUCT_TYPE_PERP, ZERO
from app.core.decimal_utils import decimal_to_str, quantize_scale, to_decimal
from app.core.time_utils import to_millis
from app.models.contract_account import ContractAccount
from app.models.contract_adl_event import ContractAdlEvent
from app.models.contract_funding_event import ContractFundingEvent
from app.models.contract_funding_job import ContractFundingJob
from app.models.contract_funding_settlement import ContractFundingSettlement
from app.models.contract_liquidation_event import ContractLiquidationEvent
from app.models.contract_market_state import ContractMarketState
from app.models.contract_position import ContractPosition
from app.models.market import Market
from app.models.user import User
from app.services.contract_adl import execute_contract_adl, serialize_adl_event
from app.services.contract_price_service import ContractPriceService
from app.services.contract_service import ContractService
from app.services.ids import next_funding_job_id
from app.services.runtime import AppRuntime


FUNDING_JOB_STALE_AFTER_SECONDS = 300
FUNDING_JOB_MAX_RETRY_SECONDS = 900


class ContractMaintenanceService:
    def __init__(
        self,
        runtime: AppRuntime,
        contract_service: ContractService,
        price_service: ContractPriceService,
    ) -> None:
        self.runtime = runtime
        self.contract_service = contract_service
        self.price_service = price_service

    @staticmethod
    def local_funding_boundary(market: Market, now: datetime) -> datetime:
        interval = max(1, int(market.funding_interval_hours or 8))
        hour = (now.hour // interval) * interval
        return now.replace(hour=hour, minute=0, second=0, microsecond=0)

    def due_funding_time(self, state: ContractMarketState, market: Market, now: datetime) -> datetime:
        if state.next_funding_time is not None:
            funding_time = state.next_funding_time
            if funding_time.tzinfo is None:
                funding_time = funding_time.replace(tzinfo=UTC)
            return funding_time
        return self.local_funding_boundary(market, now)

    @staticmethod
    def as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def next_retry_time(now: datetime, attempt_count: int) -> datetime:
        delay = min(FUNDING_JOB_MAX_RETRY_SECONDS, 60 * max(1, attempt_count))
        return now + timedelta(seconds=delay)

    async def ensure_funding_job(
        self,
        session: AsyncSession,
        market: Market,
        funding_time: datetime,
        *,
        now: datetime,
    ) -> ContractFundingJob:
        funding_time = self.as_utc(funding_time) or now
        existing = await session.scalar(
            select(ContractFundingJob).where(
                ContractFundingJob.market_id == market.id,
                ContractFundingJob.funding_time == funding_time,
            )
        )
        if existing is not None:
            return existing
        job = ContractFundingJob(
            job_id=next_funding_job_id(),
            market_id=market.id,
            funding_time=funding_time,
            status="pending",
            max_attempts=3,
        )
        try:
            async with session.begin_nested():
                session.add(job)
                await session.flush()
        except IntegrityError:
            existing = await session.scalar(
                select(ContractFundingJob).where(
                    ContractFundingJob.market_id == market.id,
                    ContractFundingJob.funding_time == funding_time,
                )
            )
            if existing is not None:
                return existing
            raise
        return job

    def funding_job_can_run(self, job: ContractFundingJob, now: datetime) -> tuple[bool, str]:
        if job.status == "settled":
            return False, "settled"
        if job.status == "running":
            heartbeat = self.as_utc(job.heartbeat_at)
            if heartbeat is not None and now - heartbeat < timedelta(seconds=FUNDING_JOB_STALE_AFTER_SECONDS):
                return False, "running"
        if job.status == "failed" and job.attempt_count >= job.max_attempts:
            return False, "max_attempts"
        next_retry_at = self.as_utc(job.next_retry_at)
        if next_retry_at is not None and next_retry_at > now:
            return False, "waiting_retry"
        return True, "ready"

    async def start_funding_job(self, session: AsyncSession, job: ContractFundingJob, now: datetime) -> ContractFundingJob:
        job.status = "running"
        job.attempt_count += 1
        job.locked_by = self.runtime.contract_funding_worker_id
        job.locked_at = now
        job.heartbeat_at = now
        job.next_retry_at = None
        job.last_error = None
        job.started_at = job.started_at or now
        job.completed_at = None
        job.updated_at = now
        await session.flush()
        await session.commit()
        return job

    async def complete_funding_job(
        self,
        session: AsyncSession,
        market: Market,
        funding_time: datetime,
        result: dict,
        now: datetime,
    ) -> ContractFundingJob:
        job = await self.ensure_funding_job(session, market, funding_time, now=now)
        job.status = "settled"
        job.settlement_id = result.get("settlement_id")
        job.last_error = None
        job.heartbeat_at = now
        job.completed_at = now
        job.next_retry_at = None
        job.updated_at = now
        await session.flush()
        return job

    async def fail_funding_job(
        self,
        session: AsyncSession,
        market: Market,
        funding_time: datetime,
        exc: Exception,
        now: datetime,
    ) -> ContractFundingJob:
        job = await self.ensure_funding_job(session, market, funding_time, now=now)
        job.status = "failed"
        job.last_error = f"{exc.__class__.__name__}: {str(exc)[:460]}"
        job.heartbeat_at = now
        job.completed_at = now
        job.next_retry_at = self.next_retry_time(now, job.attempt_count) if job.attempt_count < job.max_attempts else None
        job.updated_at = now
        await session.flush()
        return job

    async def retry_funding_job(
        self,
        session: AsyncSession,
        job_id: str,
        *,
        now: datetime | None = None,
        fetch_external: bool = True,
    ) -> dict:
        now = now or datetime.now(tz=UTC)
        row = await session.execute(
            select(ContractFundingJob, Market)
            .join(Market, Market.id == ContractFundingJob.market_id)
            .where(ContractFundingJob.job_id == job_id, Market.product_type == PRODUCT_TYPE_PERP)
        )
        item = row.first()
        if item is None:
            raise ValueError("funding job not found")
        job, market = item
        market_id = market.id
        funding_time = self.as_utc(job.funding_time) or now

        existing_settlement = await session.scalar(
            select(ContractFundingSettlement).where(
                ContractFundingSettlement.market_id == market.id,
                ContractFundingSettlement.funding_time == funding_time,
            )
        )
        if existing_settlement is not None and existing_settlement.status == "settled":
            result = {
                "symbol": market.symbol,
                "funding_time": to_millis(funding_time),
                "funding_rate": decimal_to_str(to_decimal(existing_settlement.funding_rate)),
                "settled_count": 0,
                "already_settled": True,
                "settlement_id": existing_settlement.settlement_id,
                "status": existing_settlement.status,
                "items": [],
            }
            job = await self.complete_funding_job(session, market, funding_time, result, now)
            await session.commit()
            return {"job": self.serialize_funding_job(job, market, action="already_settled"), "result": result}

        if job.status == "running":
            heartbeat = self.as_utc(job.heartbeat_at)
            if heartbeat is not None and now - heartbeat < timedelta(seconds=FUNDING_JOB_STALE_AFTER_SECONDS):
                raise ValueError("funding job is still running")

        if job.status == "settled":
            return {"job": self.serialize_funding_job(job, market, action="settled"), "result": None}

        if job.attempt_count >= job.max_attempts:
            job.max_attempts = job.attempt_count + 1
            await session.flush()

        await self.start_funding_job(session, job, now)
        try:
            result = await self.price_service.settle_funding(
                session,
                market,
                funding_time=funding_time,
                fetch_external=fetch_external,
            )
            job = await self.complete_funding_job(session, market, funding_time, result, now)
            await session.commit()
            return {"job": self.serialize_funding_job(job, market, action="retried"), "result": result}
        except Exception as exc:
            await session.rollback()
            reloaded_market = await session.get(Market, market_id)
            if reloaded_market is None:
                raise
            job = await self.fail_funding_job(session, reloaded_market, funding_time, exc, now)
            await session.commit()
            return {"job": self.serialize_funding_job(job, reloaded_market, action="failed"), "error": exc.__class__.__name__}

    async def retry_failed_funding_jobs(
        self,
        session: AsyncSession,
        *,
        symbol: str | None = None,
        limit: int = 50,
        now: datetime | None = None,
        fetch_external: bool = True,
    ) -> dict:
        now = now or datetime.now(tz=UTC)
        stmt = (
            select(ContractFundingJob.job_id)
            .join(Market, Market.id == ContractFundingJob.market_id)
            .where(Market.product_type == PRODUCT_TYPE_PERP, ContractFundingJob.status == "failed")
            .order_by(ContractFundingJob.funding_time.asc(), ContractFundingJob.job_id.asc())
            .limit(limit)
        )
        if symbol:
            stmt = stmt.where(Market.symbol == symbol.upper())
        job_ids = list((await session.execute(stmt)).scalars().all())
        items: list[dict] = []
        succeeded_count = 0
        failed_count = 0
        for retry_job_id in job_ids:
            try:
                item = await self.retry_funding_job(
                    session,
                    retry_job_id,
                    now=now,
                    fetch_external=fetch_external,
                )
            except ValueError as exc:
                item = {"job_id": retry_job_id, "error": str(exc)}
            if item.get("error"):
                failed_count += 1
            else:
                succeeded_count += 1
            items.append(item)
        return {
            "requested_count": len(job_ids),
            "succeeded_count": succeeded_count,
            "failed_count": failed_count,
            "items": items,
            "limit": limit,
            "symbol": symbol.upper() if symbol else None,
            "updated_at": to_millis(now),
        }

    def serialize_funding_job(self, job: ContractFundingJob, market: Market, *, action: str | None = None) -> dict:
        values = job.__dict__
        created_at = values.get("created_at") or values.get("started_at") or values.get("locked_at") or values.get("heartbeat_at")
        updated_at = values.get("updated_at") or values.get("completed_at") or values.get("heartbeat_at") or created_at

        def optional_millis(key: str) -> int | None:
            value = values.get(key)
            return to_millis(value) if value is not None else None

        return {
            "job_id": values.get("job_id"),
            "symbol": market.symbol,
            "funding_time": optional_millis("funding_time"),
            "status": values.get("status"),
            "attempt_count": values.get("attempt_count"),
            "max_attempts": values.get("max_attempts"),
            "locked_by": values.get("locked_by"),
            "locked_at": optional_millis("locked_at"),
            "heartbeat_at": optional_millis("heartbeat_at"),
            "next_retry_at": optional_millis("next_retry_at"),
            "settlement_id": values.get("settlement_id"),
            "last_error": values.get("last_error"),
            "started_at": optional_millis("started_at"),
            "completed_at": optional_millis("completed_at"),
            "created_at": to_millis(created_at) if created_at is not None else None,
            "updated_at": to_millis(updated_at) if updated_at is not None else None,
            "action": action,
        }

    async def funding_already_settled(self, session: AsyncSession, market: Market, funding_time: datetime) -> bool:
        funding_ms = to_millis(funding_time)
        if self.runtime.contract_auto_funding_watermarks.get(market.symbol) == funding_ms:
            return True
        settlement = await session.scalar(
            select(ContractFundingSettlement).where(
                ContractFundingSettlement.market_id == market.id,
                ContractFundingSettlement.funding_time == funding_time,
            )
        )
        if settlement is not None:
            return True
        existing = await session.scalar(
            select(func.count())
            .select_from(ContractFundingEvent)
            .where(ContractFundingEvent.market_id == market.id, ContractFundingEvent.funding_time == funding_time)
        )
        return bool(existing)

    async def refresh_positions(self, session: AsyncSession, market: Market) -> list[dict]:
        async def keys() -> list[str]:
            user_ids = (await session.execute(
                select(ContractPosition.user_id).where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                ).distinct()
            )).scalars().all()
            asset = (market.margin_asset or market.quote_asset).upper()
            return [f"contract:{int(user_id)}:{asset}" for user_id in sorted(user_ids)]

        async with self.runtime.market_financial_guard(market.symbol, keys):
            rows = (await session.execute(
                select(ContractPosition, ContractAccount, User)
                .join(ContractAccount, ContractAccount.user_id == ContractPosition.user_id)
                .join(User, User.id == ContractPosition.user_id)
                .where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                    ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
                )
            )).all()
            accounts = {account.user_id: account for _, account, _ in rows}
            now = datetime.now(tz=UTC)
            for account in accounts.values():
                await self.contract_service.refresh_account_with_ledger(
                    session,
                    account,
                    market=market,
                    note="contract_maintenance_mark_to_market",
                    created_at=now,
                )
            alerts: list[dict] = []
            for position, _account, user in rows:
                risk = self.contract_service.position_risk(position, market)
                if risk["risk_status"] not in {"flat", "ok"}:
                    alerts.append(
                        {
                            "symbol": market.symbol,
                            "user_id": user.id,
                            "username": user.username,
                            "side": position.side,
                            "quantity": decimal_to_str(to_decimal(position.quantity)),
                            "mark_price": decimal_to_str(quantize_scale(position.mark_price, market.price_precision)),
                            "liquidation_price": decimal_to_str(quantize_scale(position.liquidation_price, market.price_precision)),
                            "liquidation_distance_pct": risk["liquidation_distance_pct"],
                            "margin_buffer": risk["margin_buffer"],
                            "risk_status": risk["risk_status"],
                        }
                    )
            return alerts

    async def liquidate_due_positions(self, session: AsyncSession, market: Market, *, now: datetime) -> list[ContractLiquidationEvent]:
        async def keys() -> list[str]:
            user_ids = (await session.execute(
                select(ContractPosition.user_id).where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                ).distinct()
            )).scalars().all()
            asset = (market.margin_asset or market.quote_asset).upper()
            return [f"contract:{int(user_id)}:{asset}" for user_id in sorted(user_ids)] + [f"insurance:{asset}"]

        async with self.runtime.market_financial_guard(market.symbol, keys):
            rows = await session.execute(
                select(ContractPosition, ContractAccount)
                .join(ContractAccount, ContractAccount.user_id == ContractPosition.user_id)
                .where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                    ContractAccount.margin_asset == (market.margin_asset or market.quote_asset),
                )
            )
            events: list[ContractLiquidationEvent] = []
            for position, account in rows.all():
                event = await self.contract_service.liquidate_position(
                    session,
                    market=market,
                    position=position,
                    account=account,
                    now=now,
                )
                if event is not None:
                    events.append(event)
            return events

    async def broadcast_liquidations(self, session: AsyncSession, market: Market, events: list[ContractLiquidationEvent]) -> None:
        for event in events:
            account = await self.contract_service.serialize_account(session, event.user_id, market.margin_asset or market.quote_asset)
            position_rows = await session.execute(
                select(ContractPosition).where(
                    ContractPosition.user_id == event.user_id,
                    ContractPosition.market_id == market.id,
                    *ContractPosition.active_filters(),
                )
            )
            positions = [
                await self.contract_service.serialize_position(position, market, session)
                for position in position_rows.scalars().all()
            ]
            await self.runtime.ws.broadcast_private(
                event.user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "snapshot",
                    "account": account,
                    "positions": positions,
                    "ts": to_millis(datetime.now(tz=UTC)),
                },
            )
            await self.runtime.ws.broadcast_private(
                event.user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "liquidation",
                    "data": self.contract_service.serialize_liquidation_event(event, market),
                    "ts": to_millis(datetime.now(tz=UTC)),
                },
            )

    async def process_pending_adl(
        self,
        session: AsyncSession,
        market: Market,
        *,
        now: datetime,
        limit: int = 50,
        max_candidates: int = 20,
    ) -> tuple[list[dict], list[ContractAdlEvent], list[ContractLiquidationEvent]]:
        async def keys() -> list[str]:
            user_ids = (await session.execute(
                select(ContractPosition.user_id).where(
                    ContractPosition.market_id == market.id,
                    ContractPosition.quantity > ZERO,
                ).distinct()
            )).scalars().all()
            asset = (market.margin_asset or market.quote_asset).upper()
            return [f"contract:{int(user_id)}:{asset}" for user_id in sorted(user_ids)] + [f"insurance:{asset}"]

        async with self.runtime.market_financial_guard(market.symbol, keys):
            return await self._process_pending_adl_locked(
                session,
                market,
                now=now,
                limit=limit,
                max_candidates=max_candidates,
            )

    async def _process_pending_adl_locked(
        self,
        session: AsyncSession,
        market: Market,
        *,
        now: datetime,
        limit: int,
        max_candidates: int,
    ) -> tuple[list[dict], list[ContractAdlEvent], list[ContractLiquidationEvent]]:
        rows = await session.execute(
            select(ContractLiquidationEvent)
            .where(
                ContractLiquidationEvent.market_id == market.id,
                ContractLiquidationEvent.adl_status.in_(["pending", "partial"]),
                ContractLiquidationEvent.adl_residual > ZERO,
            )
            .order_by(ContractLiquidationEvent.liquidated_at.asc(), ContractLiquidationEvent.event_id.asc())
            .limit(limit)
        )
        items: list[dict] = []
        emitted_adl_events: list[ContractAdlEvent] = []
        updated_liquidations: list[ContractLiquidationEvent] = []
        for liquidation_event in rows.scalars().all():
            result = await execute_contract_adl(
                session,
                self.contract_service,
                liquidation_event,
                market,
                max_candidates=max_candidates,
                now=now,
            )
            adl_events = result.get("events", [])
            emitted_adl_events.extend(adl_events)
            updated_liquidations.append(liquidation_event)
            items.append(
                {
                    "symbol": market.symbol,
                    "liquidation_event_id": liquidation_event.event_id,
                    "event_count": len(adl_events),
                    "covered_amount": result["covered_amount"],
                    "residual_after": result["residual_after"],
                    "status": result["status"],
                }
            )
        return items, emitted_adl_events, updated_liquidations

    async def broadcast_adl_events(
        self,
        session: AsyncSession,
        market: Market,
        adl_events: list[ContractAdlEvent],
        liquidation_events: list[ContractLiquidationEvent],
        *,
        now: datetime,
    ) -> None:
        """Publish only facts that have already been committed by run_once()."""
        for adl_event in adl_events:
            account = await self.contract_service.serialize_account(
                session, adl_event.user_id, market.margin_asset or market.quote_asset
            )
            position_rows = await session.execute(
                select(ContractPosition).where(
                    ContractPosition.user_id == adl_event.user_id,
                    ContractPosition.market_id == market.id,
                    *ContractPosition.active_filters(),
                )
            )
            positions = [
                await self.contract_service.serialize_position(position, market, session)
                for position in position_rows.scalars().all()
            ]
            await self.runtime.ws.broadcast_private(
                adl_event.user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "snapshot",
                    "account": account,
                    "positions": positions,
                    "ts": to_millis(now),
                },
            )
            await self.runtime.ws.broadcast_private(
                adl_event.user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "adl",
                    "data": serialize_adl_event(adl_event, market.symbol),
                    "ts": to_millis(now),
                },
            )
        for liquidation_event in liquidation_events:
            await self.runtime.ws.broadcast_private(
                liquidation_event.user_id,
                "contracts",
                {
                    "channel": "contracts",
                    "type": "liquidation",
                    "data": self.contract_service.serialize_liquidation_event(liquidation_event, market),
                    "ts": to_millis(now),
                },
            )

    async def run_once(
        self,
        session: AsyncSession,
        *,
        now: datetime | None = None,
        fetch_external: bool = True,
        auto_settle_funding: bool = True,
        auto_liquidate: bool = True,
        auto_adl: bool = True,
    ) -> dict:
        now = now or datetime.now(tz=UTC)
        rows = await session.execute(
            select(Market).where(Market.product_type == PRODUCT_TYPE_PERP, Market.is_active.is_(True)).order_by(Market.symbol.asc())
        )
        metrics: dict = {
            "updated_at": to_millis(now),
            "markets": 0,
            "price_refreshes": 0,
            "auto_settle_enabled": auto_settle_funding,
            "auto_liquidation_enabled": auto_liquidate,
            "auto_adl_enabled": auto_adl,
            "fetch_external": fetch_external,
            "funding_settlements": [],
            "funding_jobs": [],
            "funding_job_failures": 0,
            "liquidations": [],
            "liquidation_count": 0,
            "adl_events": [],
            "adl_event_count": 0,
            "risk_alert_count": 0,
            "errors": [],
        }
        risk_alerts: dict[str, list[dict]] = {}
        for market in rows.scalars():
            market_symbol = market.symbol
            market_id = market.id
            metrics["markets"] += 1
            try:
                state = await self.price_service.refresh_market_state(session, market, fetch_external=fetch_external)
                metrics["price_refreshes"] += 1
                alerts = await self.refresh_positions(session, market)
                liquidation_events: list[ContractLiquidationEvent] = []
                if auto_liquidate:
                    liquidation_events = await self.liquidate_due_positions(session, market, now=now)
                    if liquidation_events:
                        # Financial facts are final only after commit. WebSocket
                        # notification is best-effort and must never expose an
                        # uncommitted liquidation or roll back committed money.
                        await session.commit()
                        try:
                            await self.broadcast_liquidations(session, market, liquidation_events)
                        except Exception as exc:
                            await session.rollback()
                            metrics["errors"].append(
                                {"symbol": market_symbol, "stage": "liquidation_broadcast", "error": exc.__class__.__name__}
                            )
                        alerts = await self.refresh_positions(session, market)
                        metrics["liquidation_count"] += len(liquidation_events)
                        metrics["liquidations"].extend(
                            [
                                {
                                    "symbol": market.symbol,
                                    "event_id": event.event_id,
                                    "user_id": event.user_id,
                                    "position_side": event.position_side,
                                    "quantity": decimal_to_str(to_decimal(event.quantity)),
                                    "mark_price": decimal_to_str(quantize_scale(event.mark_price, market.price_precision)),
                                    "realized_pnl": decimal_to_str(quantize_scale(event.realized_pnl, 8)),
                                    "liquidated_at": to_millis(event.liquidated_at),
                                }
                                for event in liquidation_events
                            ]
                        )
                if auto_liquidate and auto_adl:
                    adl_items, adl_events, adl_liquidation_events = await self.process_pending_adl(
                        session, market, now=now
                    )
                    if adl_items:
                        await session.commit()
                        try:
                            await self.broadcast_adl_events(
                                session,
                                market,
                                adl_events,
                                adl_liquidation_events,
                                now=now,
                            )
                        except Exception as exc:
                            await session.rollback()
                            metrics["errors"].append(
                                {"symbol": market_symbol, "stage": "adl_broadcast", "error": exc.__class__.__name__}
                            )
                        alerts = await self.refresh_positions(session, market)
                        metrics["adl_events"].extend(adl_items)
                        metrics["adl_event_count"] += sum(int(item.get("event_count") or 0) for item in adl_items)
                risk_alerts[market.symbol] = alerts
                metrics["risk_alert_count"] += len(alerts)

                funding_time = self.due_funding_time(state, market, now)
                if auto_settle_funding and funding_time <= now and not await self.funding_already_settled(session, market, funding_time):
                    job = await self.ensure_funding_job(session, market, funding_time, now=now)
                    can_run, reason = self.funding_job_can_run(job, now)
                    if not can_run:
                        metrics["funding_jobs"].append(self.serialize_funding_job(job, market, action=reason))
                    else:
                        await self.start_funding_job(session, job, now)
                        try:
                            result = await self.price_service.settle_funding(
                                session,
                                market,
                                funding_time=funding_time,
                                fetch_external=False,
                            )
                            self.runtime.contract_auto_funding_watermarks[market.symbol] = to_millis(funding_time)
                            job = await self.complete_funding_job(session, market, funding_time, result, now)
                            metrics["funding_settlements"].append(
                                {
                                    "symbol": market.symbol,
                                    "funding_time": to_millis(funding_time),
                                    "funding_rate": result["funding_rate"],
                                    "settled_count": result["settled_count"],
                                }
                            )
                            metrics["funding_jobs"].append(self.serialize_funding_job(job, market, action="settled"))
                        except Exception as exc:
                            await session.rollback()
                            reloaded_market = await session.get(Market, market_id)
                            if reloaded_market is None:
                                raise
                            job = await self.fail_funding_job(session, reloaded_market, funding_time, exc, now)
                            await session.commit()
                            metrics["funding_job_failures"] += 1
                            metrics["funding_jobs"].append(self.serialize_funding_job(job, reloaded_market, action="failed"))
                            metrics["errors"].append({"symbol": market_symbol, "stage": "funding", "error": exc.__class__.__name__})
            except Exception as exc:
                await session.rollback()
                metrics["errors"].append({"symbol": market_symbol, "error": exc.__class__.__name__})
        self.runtime.contract_risk_alerts = risk_alerts
        self.runtime.contract_maintenance_metrics = metrics
        await session.commit()
        return metrics

    def serialize_runtime_status(self) -> dict:
        return {
            "metrics": self.runtime.contract_maintenance_metrics,
            "risk_alerts": self.runtime.contract_risk_alerts,
        }
