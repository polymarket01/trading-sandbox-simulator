from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED, ORDER_TYPE_LIMIT, TIF_GTC
from app.core.config import settings
from app.core.time_utils import ensure_utc, to_millis
from app.db.sqlite_observability import sqlite_observability_snapshot
from app.models.market import Market
from app.models.market_maker_instance import MarketMakerInstance
from app.models.order import Order
from app.models.trade import Trade
from app.models.user import User
from app.services.runtime import AppRuntime


LIVE_ORDER_STATUSES = (ORDER_STATUS_NEW, ORDER_STATUS_PARTIALLY_FILLED)
LOCK_ERROR_RE = re.compile(r"database is locked", re.IGNORECASE)
TIMEOUT_RE = re.compile(r"ReadTimeout|TimeoutError|timed out", re.IGNORECASE)
RESTART_RE = re.compile(r"stop pid=|自动恢复|maker-instance/restart", re.IGNORECASE)


def _process_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False
    return True


def _age_ms(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - ensure_utc(value)).total_seconds() * 1000))


def _depth_notional(levels: list[list[str]], mid: Decimal, pct: Decimal) -> str:
    if mid <= 0:
        return "0"
    total = Decimal("0")
    for raw_price, raw_quantity in levels:
        price = Decimal(str(raw_price))
        if abs(price - mid) / mid <= pct:
            total += price * Decimal(str(raw_quantity))
    return format(total, "f")


class LiquidityDiagnosticsService:
    """Read-only, on-demand evidence aggregator; it never repairs state."""

    def __init__(self, runtime: AppRuntime, *, runtime_dir: Path | None = None) -> None:
        self.runtime = runtime
        self.runtime_dir = runtime_dir or Path(__file__).resolve().parents[3] / ".runtime"

    def _log_evidence(self, symbol: str, instance: MarketMakerInstance | None) -> dict:
        paths = [self.runtime_dir / f"mm_service_{symbol}.log"]
        run_meta = (
            (instance.last_metrics_json or {}).get("_run", {})
            if instance is not None and isinstance(instance.last_metrics_json, dict)
            else {}
        )
        start_offset = int(run_meta.get("log_start_offset") or 0)
        text = ""
        for path in paths:
            try:
                with path.open("rb") as handle:
                    handle.seek(start_offset)
                    text += handle.read().decode("utf-8", errors="replace")
            except OSError:
                continue
        sqlite_metrics = sqlite_observability_snapshot()
        return {
            "recent_database_lock_errors": int(sqlite_metrics["database_lock_errors"]),
            "last_database_lock_at_ms": sqlite_metrics["last_database_lock_at_ms"],
            "recent_api_timeouts": len(TIMEOUT_RE.findall(text)),
            "recent_restart_markers": len(RESTART_RE.findall(text)),
            "window": "current_backend_process_and_current_maker_run",
        }

    async def diagnose_market(self, session: AsyncSession, market: Market) -> dict:
        started_at = datetime.now(tz=UTC)
        symbol = market.symbol.upper()
        snapshot, seq, updated_at_ms = await self.runtime.orderbook_snapshot(symbol, 100)
        bids = snapshot.get("bids", [])
        asks = snapshot.get("asks", [])
        best_bid = Decimal(bids[0][0]) if bids else None
        best_ask = Decimal(asks[0][0]) if asks else None
        mid = ((best_bid + best_ask) / 2) if best_bid is not None and best_ask is not None else Decimal("0")
        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None

        async with self.runtime.market_locks[symbol]:
            db_query = select(Order.order_id).where(
                Order.market_id == market.id,
                Order.type == ORDER_TYPE_LIMIT,
                Order.status.in_(LIVE_ORDER_STATUSES),
                Order.remaining_quantity > 0,
                Order.tif == TIF_GTC,
            )
            if settings.persistence_mode == "memory":
                db_query = db_query.join(User, User.id == Order.user_id).where(User.role != "mm_bot")
            db_rows = await session.execute(db_query)
            db_order_ids = set(db_rows.scalars())
            engine_book = self.runtime.engine.ensure_market(symbol)
            if settings.persistence_mode == "memory":
                role_rows = await session.execute(select(User.id, User.role))
                user_role = {int(uid): role for uid, role in role_rows.all()}
                engine_order_ids = {
                    order_id
                    for order_id, node in engine_book.orders.items()
                    if user_role.get(int(node.user_id)) != "mm_bot"
                }
            else:
                engine_order_ids = set(engine_book.orders)
        instance = await session.scalar(
            select(MarketMakerInstance).where(MarketMakerInstance.market_id == market.id)
        )
        last_trade_at, last_trade_id = (await session.execute(
            select(Trade.executed_at, Trade.trade_id)
            .where(Trade.market_id == market.id)
            .order_by(Trade.executed_at.desc(), Trade.id.desc())
            .limit(1)
        )).first() or (None, None)
        last_flow_trade_at = await session.scalar(
            select(func.max(Trade.executed_at)).where(Trade.market_id == market.id, Trade.source == "flow")
        )
        order_count, last_order_at = (await session.execute(
            select(func.count(Order.id), func.max(Order.created_at)).where(Order.market_id == market.id)
        )).one()
        trade_count, max_trade_at = (await session.execute(
            select(func.count(Trade.id), func.max(Trade.executed_at)).where(Trade.market_id == market.id)
        )).one()

        metrics = dict(self.runtime.liquidity_metrics.get(symbol, {}) or {})
        flow = metrics.get("flow_ioc") if isinstance(metrics.get("flow_ioc"), dict) else {}
        flow_allowed_now = metrics.get("flow_allowed")
        if flow_allowed_now is None:
            flow_allowed_now = flow.get("allowed")
        flow_status_now = metrics.get("flow_status") or flow.get("status") or flow.get("last_status")
        target_levels = int(
            metrics.get("target_levels_per_side")
            or metrics.get("levels_per_side")
            or max(len(bids), len(asks), 1)
        )
        completion = min(len(bids), len(asks)) / max(target_levels, 1)
        now = datetime.now(tz=UTC)
        book_age_ms = max(0, to_millis(now) - int(updated_at_ms or 0)) if updated_at_ms else None
        heartbeat_age_ms = _age_ms(instance.last_heartbeat_at if instance else None, now)
        process_alive = _process_alive(instance.pid if instance else None)
        diff_engine_only = sorted(engine_order_ids - db_order_ids)[:20]
        diff_db_only = sorted(db_order_ids - engine_order_ids)[:20]
        logs = self._log_evidence(symbol, instance)
        lock_error_recent = bool(
            logs["last_database_lock_at_ms"]
            and to_millis(now) - int(logs["last_database_lock_at_ms"]) <= 60_000
        )

        reasons: list[str] = []
        if not bids or not asks:
            reasons.append("empty_or_one_sided_book")
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            reasons.append("crossed_book")
        if diff_engine_only or diff_db_only:
            reasons.append("engine_database_live_order_diff")
        if not process_alive:
            reasons.append("maker_process_not_alive")
        if heartbeat_age_ms is None or heartbeat_age_ms > 10_000:
            reasons.append("heartbeat_stale")
        if book_age_ms is None or book_age_ms > 5_000:
            reasons.append("orderbook_stale")
        if lock_error_recent:
            reasons.append("recent_database_lock_contention")

        hard_failure = any(reason in reasons for reason in ("crossed_book", "engine_database_live_order_diff"))
        status = "failed" if hard_failure else ("degraded" if reasons else "healthy")
        flow_allowed = status == "healthy" and completion >= 0.95
        finished_at = datetime.now(tz=UTC)
        return {
            "run_id": str(uuid4()),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            "market": symbol,
            "product_type": market.product_type,
            "data_watermark": {
                "order_count": int(order_count or 0),
                "trade_count": int(trade_count or 0),
                "last_order_at": ensure_utc(last_order_at).isoformat() if last_order_at else None,
                "last_trade_at": ensure_utc(max_trade_at).isoformat() if max_trade_at else None,
            },
            "open_orders": {
                "engine": len(engine_order_ids),
                "database": len(db_order_ids),
                "engine_only_count": len(engine_order_ids - db_order_ids),
                "database_only_count": len(db_order_ids - engine_order_ids),
            },
            "orderbook": {
                "bid_levels": len(bids),
                "ask_levels": len(asks),
                "best_bid": str(best_bid) if best_bid is not None else None,
                "best_ask": str(best_ask) if best_ask is not None else None,
                "spread": str(spread) if spread is not None else None,
                "depth_0_1_pct": {"bids": _depth_notional(bids, mid, Decimal("0.001")), "asks": _depth_notional(asks, mid, Decimal("0.001"))},
                "depth_0_5_pct": {"bids": _depth_notional(bids, mid, Decimal("0.005")), "asks": _depth_notional(asks, mid, Decimal("0.005"))},
                "depth_1_pct": {"bids": _depth_notional(bids, mid, Decimal("0.01")), "asks": _depth_notional(asks, mid, Decimal("0.01"))},
                "target_levels_per_side": target_levels,
                "target_depth_completion": round(completion, 4),
                "seq": seq,
                "updated_at_ms": updated_at_ms,
                "age_ms": book_age_ms,
                "source": "last_committed_immutable_snapshot",
            },
            "consistency": {
                **self.runtime.market_data.orderbook_invariant_snapshot(symbol),
                **self.runtime.orderbook_reconcile_snapshot(symbol),
                "rest_ws_admin_source": "last_committed_immutable_snapshot",
                "reconciliation_clean": not diff_engine_only and not diff_db_only,
            },
            "maker": {
                "process_alive": process_alive,
                "persisted_status": instance.status if instance else "missing",
                "heartbeat_age_ms": heartbeat_age_ms,
                "last_quote_at": metrics.get("last_successful_quote_at") or metrics.get("ts"),
            },
            "flow": {
                "allowed": flow_allowed_now,
                "status": flow_status_now,
                "last_trade_at": ensure_utc(last_flow_trade_at).isoformat() if last_flow_trade_at else None,
                "recommend_continue": flow_allowed,
            },
            "activity": {
                "last_trade_id": last_trade_id,
                "last_trade_at": ensure_utc(last_trade_at).isoformat() if last_trade_at else None,
            },
            "recent_faults": logs,
            "status": status,
            "reasons": reasons,
            "recommend_pause_auto_restart": bool(lock_error_recent and process_alive),
            "diff_sample": {"engine_only": diff_engine_only, "database_only": diff_db_only},
            "suggested_action": (
                "pause_flow_and_investigate_database_contention" if lock_error_recent
                else "reconcile_engine_from_committed_database" if hard_failure
                else "observe" if status == "healthy"
                else "inspect_maker_and_book_freshness"
            ),
        }

    async def diagnose(self, session: AsyncSession, *, symbol: str | None = None) -> dict:
        stmt = select(Market).order_by(Market.symbol.asc())
        if symbol:
            stmt = stmt.where(Market.symbol == symbol.upper())
        markets = list((await session.execute(stmt)).scalars())
        return {
            "read_only": True,
            "items": [await self.diagnose_market(session, market) for market in markets],
        }
