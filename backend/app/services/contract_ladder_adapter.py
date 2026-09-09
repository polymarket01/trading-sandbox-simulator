"""普通 limit 挂撤接入；内部身份不从订单标签取得，用户成交原事务持久化。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4
from time import monotonic

from sqlalchemy import select

from app.core.time_utils import to_millis
from app.models.market_bot_account import MarketBotAccount
from app.models.order import Order
from app.models.user import User
from app.schemas.api import ContractOrderCreateRequest
from app.services.contract_service import ContractValidationError
from app.services.matching_engine import EngineResult
from app.services.sequencer import CancelOrderCommand, NewOrderCommand
from app.services.sqlite_write_admission import acquire_sqlite_write_admission, WriteAdmissionRejected


class LadderAdmissionRejected(WriteAdmissionRejected, ContractValidationError):
    """A quote failed identity/validation before reaching the matcher."""


class QuoteSuperseded(WriteAdmissionRejected):
    """The originating target expired before any matching mutation."""
    quote_superseded = True


class ContractLadderAdapter:
    """One in-process writer per symbol; no hidden order queue or stronger order type.

    A returned order is an engine result, never merely accepted. An interrupted
    caller can query the same request while shielded execution finishes. Quote
    recovery relies on the existing exclusive SQLite process guard; it is not a
    distributed Kafka barrier and does not claim to be one.
    """
    def __init__(self, runtime, contract_service, session_factory):
        self.runtime = runtime
        self.service = contract_service
        self.session_factory = session_factory
        self._requests = OrderedDict()
        self._inflight = {}
        self._fingerprints = OrderedDict()
        self._matched_requests = set()
        self._identity_cache = {}
        self._metadata_epochs = {}
        self._cancel_inflight = {}
        self._matched_cancels = set()

    def invalidate_metadata(self, symbol: str) -> int:
        """Drop this market's precision snapshots when its full version changes.

        Identity bindings and order/request state are independent of precision
        and are retained. ContractService holds no additional metadata cache.
        An older read already in flight cannot repopulate the invalidated cache.
        """
        normalized = str(symbol).strip().upper()
        self._metadata_epochs[normalized] = self._metadata_epochs.get(normalized, 0) + 1
        keys = [key for key in self._identity_cache if key[0] == normalized]
        for key in keys:
            self._identity_cache.pop(key, None)
        return len(keys)

    def invalidate_identity(self, symbol, market_id):
        self.invalidate_metadata(symbol)
        self.service.invalidate_internal_maker_bindings(market_id)

    async def _identity(self, session, symbol, uid, *, placing=False, fresh=False):
        cache_key = (str(symbol).strip().upper(), int(uid))
        metadata_epoch = self._metadata_epochs.get(cache_key[0], 0)
        cached = self._identity_cache.get(cache_key)
        if not fresh and cached is not None and monotonic() - cached[0] < 1.0:
            _at, market, user, enabled = cached
            if placing and (not enabled or not user.is_active):
                raise LadderAdmissionRejected("internal maker identity is disabled")
            return market, user
        market = await self.service.get_market(session, str(symbol).upper())
        user = await session.get(User, int(uid))
        if user is None or user.role != "mm_bot" or not await self.service.is_contract_ladder(session, int(uid), int(market.id)):
            raise LadderAdmissionRejected("CONTRACT_LADDER server binding required")
        enabled = await session.scalar(select(MarketBotAccount.is_enabled).where(
            MarketBotAccount.market_id == market.id, MarketBotAccount.user_id == int(uid),
            MarketBotAccount.strategy_role == "CONTRACT_LADDER"))
        # Detached read-only metadata snapshot, refreshed at most one second old.
        # Never reuse a live session's transaction/identity map across actions.
        session.expunge(market)
        session.expunge(user)
        if self._metadata_epochs.get(cache_key[0], 0) == metadata_epoch:
            self._identity_cache[cache_key] = (monotonic(), market, user, bool(enabled))
        if placing and (not enabled or not user.is_active):
            raise LadderAdmissionRejected("internal maker identity is disabled")
        return market, user

    async def _resource_keys(self, market, uid):
        users = {int(o.user_id) for o in self.runtime.engine.ensure_market(market.symbol).orders.values()}
        users.add(int(uid))
        asset = str(market.margin_asset or market.quote_asset).upper()
        return [f"contract:{u}:{asset}" for u in sorted(users)]

    def _ready(self, symbol, *, cancel=False):
        self.runtime.engine.fault.check()
        if not self.service._fast_writer_ready():
            raise ContractValidationError("durable financial writer is unavailable or halted")
        if not cancel and symbol in self.service._ladder_financial_unknown:
            raise ContractValidationError("UNKNOWN user financial transaction; quoting paused")

    async def place_checked(self, symbol, maker_uid, *, admission_check, **order):
        """Keep the originating worker's quote validity across storage waits."""
        return await self.place(symbol, maker_uid, admission_check=admission_check, **order)

    async def place(self, symbol, maker_uid, *, client_order_id, side, price, quantity, admission_check=None):
        key = (str(symbol).upper(), int(maker_uid), str(client_order_id))
        fingerprint = (str(side), Decimal(str(price)), Decimal(str(quantity)))
        if key in self._fingerprints and self._fingerprints[key] != fingerprint:
            raise ContractValidationError("client_order_id conflicts with original order")
        if key in self._requests:
            return deepcopy(self._requests[key])
        task = self._inflight.get(key)
        if task is None:
            self.runtime.engine.fault.check()
            self._fingerprints[key] = fingerprint
            execution = (self._place(key, side, price, quantity) if admission_check is None else
                         self._place(key, side, price, quantity, admission_check=admission_check))
            task = asyncio.create_task(execution, name=f"ladder-order-{key[0]}")
            self._inflight[key] = task
            def finish(done):
                self._inflight.pop(key, None)
                if not done.cancelled() and done.exception() is None:
                    self._requests[key] = deepcopy(done.result())
                elif key in self._matched_requests:
                    self.service._ladder_financial_unknown[key[0]] = "request failed after matcher: " + key[2]
                    self.runtime.engine.fault.halt("internal maker result unknown: " + key[2])
                self._matched_requests.discard(key)
                while len(self._requests) > 4096:
                    old, _ = self._requests.popitem(last=False)
                    self._fingerprints.pop(old, None)
                while len(self._fingerprints) > 8192:
                    old = next(iter(self._fingerprints))
                    if old in self._inflight:
                        break
                    self._fingerprints.pop(old, None)
            task.add_done_callback(finish)
        return await asyncio.shield(task)

    def _maker_snapshots(self, fills):
        mirrors = self.service._fast_contract_orders
        return {oid: dict(mirrors[oid]) for oid in {fill.maker_order_id for fill in fills} if oid in mirrors}

    @staticmethod
    def _has_own_opposite_conflict(book, uid, side, price):
        # Check every crossed opposite level, even beyond the requested size.
        # A user wall in front must not conceal a deeper own-order conflict.
        levels = book.asks if side == "buy" else book.bids
        prices = levels.irange(maximum=price) if side == "buy" else levels.irange(minimum=price, reverse=True)
        for level_price in prices:
            node = levels[level_price].head
            while node is not None:
                if node.user_id == uid:
                    return True
                node = node.next
        return False

    def _may_fill_user(self, book, market_id, side, price):
        # Only traverses crossing opposite levels. Unknown identities are
        # conservatively financial; positive internal bindings need no SQL.
        opposite = "sell" if side == "buy" else "buy"
        for level_price, level in book._iter_levels(opposite):
            if (side == "buy" and level_price > price) or (side == "sell" and level_price < price):
                break
            node = level.head
            while node is not None:
                if (int(node.user_id), int(market_id)) not in self.service._ladder_bindings:
                    return True
                node = node.next
        return False

    @staticmethod
    def _check_quote(admission_check):
        if admission_check is not None and not admission_check():
            raise QuoteSuperseded("QUOTE_SUPERSEDED: matching command not executed")

    async def _validate_before_match(self, session, user, market, payload):
        try:
            await self.service.validate_order(session, user, market, payload)
        except ContractValidationError as exc:
            raise LadderAdmissionRejected(str(exc)) from exc

    def _preview_user_fills(self, book, market, user, payload):
        context = self.service._stp_context(user)
        preview = book.preview_bbo_order(
            side=payload.side, quantity=payload.quantity, limit_price=payload.price,
            taker_account_key=context["stp_account_key"], taker_is_bot=context["stp_is_bot"],
            stp_policy=context["stp_policy"],
            maker_guard=lambda _oid, uid, _price, _leaves: self.service._contract_maker_margin_ok(uid, int(market.id)),
        )
        # The existing preview stops on an unauthorized maker; matching would
        # remove it and continue. Reject this request before either operation,
        # rather than miss later financial fills or silently cancel user walls.
        if preview.stop_reason == "maker_not_authorized_for_synthetic_flow":
            raise LadderAdmissionRejected("maker no longer passes margin guard; matching command not executed")
        return [fill for fill in preview.fills
                if (int(fill.maker_user_id), int(market.id)) not in self.service._ladder_bindings]

    async def _place(self, key, side, price, quantity, admission_check=None):
        symbol, uid, client_id = key
        order_id = f"clord_{uuid4().hex}"
        async with self.session_factory() as session:
            market, user = await self._identity(session, symbol, uid, placing=True)
            payload = ContractOrderCreateRequest(symbol=symbol, side=side, type="limit", tif="gtc",
                price=Decimal(str(price)), quantity=Decimal(str(quantity)), client_order_id=client_id,
                position_action="open", reduce_only=False)
            async with self.runtime.market_financial_guard(symbol, lambda: self._resource_keys(market, uid)):
                self._ready(symbol)
                self._check_quote(admission_check)
                await self._validate_before_match(session, user, market, payload)
                # Stable identity also covers a caller recreated inside this process.
                prior = self.service._fast_contract_client_ids.get((uid, int(market.id), client_id))
                if prior:
                    previous = self.service._fast_contract_orders[prior]
                    if previous["side"] != side or Decimal(previous["price"]) != payload.price or Decimal(previous["quantity"]) != payload.quantity:
                        raise ContractValidationError("client_order_id conflicts with original order")
                    return {"order": self.service._fast_serialize_order(market, previous), "idempotent": True}
                now = datetime.now(tz=UTC)
                book = self.runtime.engine.ensure_market(symbol)
                if self._has_own_opposite_conflict(book, uid, side, payload.price):
                    raise ContractValidationError("own opposite order conflict; cancel confirmation required")
                financial_preview = (self._preview_user_fills(book, market, user, payload)
                                     if self._may_fill_user(book, market.id, side, payload.price) else [])
                if financial_preview:
                    # User facts must have a writable transaction BEFORE any
                    # matcher leaves change. Passive/internal-only animation
                    # remains ephemeral and never enters this storage path.
                    try:
                        # Reject already-unsettleable user walls without
                        # competing for SQLite's single writer. This early
                        # read is only a rejection filter, never permission
                        # to match after the storage wait.
                        try:
                            await self.service.preflight_ladder_user_fills(session, market, financial_preview)
                        except ContractValidationError:
                            pass  # Advisory only; final platform lifecycle owns stale leaves.
                        self._check_quote(admission_check)
                        await acquire_sqlite_write_admission(session)
                        self._ready(symbol)
                        self._check_quote(admission_check)
                        # This private session has made no ORM writes. Its
                        # precheck may retain Order objects; force the later
                        # queries to refresh them after an independent writer.
                        session.expire_all()
                        market, user = await self._identity(session, symbol, uid, placing=True, fresh=True)
                        await self._validate_before_match(session, user, market, payload)
                        try:
                            await self.service.prepare_resting_fills(
                                session, market, lambda: self._preview_user_fills(book, market, user, payload)
                            )
                            market, user = await self._identity(session, symbol, uid, placing=True, fresh=True)
                            await self._validate_before_match(session, user, market, payload)
                        except ContractValidationError as exc:
                            raise LadderAdmissionRejected(str(exc)) from exc
                        self._check_quote(admission_check)
                    except BaseException:
                        # Release SQLite ownership before the market/account
                        # guard, including rejected controls or storage waits.
                        await session.rollback()
                        raise
                self._check_quote(admission_check)
                try:
                    sequenced = await self.runtime.get_symbol_sequencer(symbol).submit(NewOrderCommand(
                        order_id=order_id, user_id=uid, side=side, quantity=payload.quantity, created_at=now,
                        limit_price=payload.price, can_rest=True,
                        maker_guard=lambda _id, maker_uid: self.service._contract_maker_margin_ok(maker_uid, int(market.id)),
                        **self.service._stp_context(user)))
                except BaseException:
                    await session.rollback()
                    raise
                self._matched_requests.add(key)
                result = sequenced.engine_result
                if result is None:
                    self.service._ladder_financial_unknown[symbol] = order_id
                    raise ContractValidationError("UNKNOWN: missing sequencer result")
                # Matcher never mutates mirrors. The financial guard is still
                # owned; no await/settlement occurs before this fill-only copy.
                before = self._maker_snapshots(result.fills)
                snap = {"order_id": order_id, "client_order_id": client_id, "user_id": uid,
                    "market_id": int(market.id), "symbol": symbol, "product_type": "PERP", "side": side,
                    "position_action": "open", "reduce_only": False, "leverage": "1", "type": "limit", "tif": "gtc",
                    "status": "new", "sequence_number": sequenced.sequence_number, "version": 0,
                    "price": str(payload.price), "quantity": str(payload.quantity), "remaining_quantity": str(payload.quantity),
                    "filled_quantity": "0", "notional": "0", "avg_price": None, "reference_price": str(market.reference_price or payload.price),
                    "reject_reason": None, "created_at": to_millis(now), "updated_at": to_millis(now)}
                internal_fills, financial_fills = [], []
                for fill in result.fills:
                    target = internal_fills if await self.service.is_contract_ladder(session, int(fill.maker_user_id), market.id) else financial_fills
                    target.append(fill)
                trades, impacted = [], {uid}
                try:
                    if financial_fills:
                        taker_order = await self.service.ensure_ladder_order_anchor(session, market, snap)
                        finance_result = EngineResult(fills=financial_fills, remaining_quantity=result.remaining_quantity, placed_on_book=result.placed_on_book)
                        updated, impacted, trades, _ = await self.service.apply_engine_fills(session=session, market=market,
                            taker_user=user, taker_order=taker_order, result=finance_result, executed_at=now, ingest=False)
                        # Use actual total fill/leaves even when internal and user fills coexist.
                        total_qty = payload.quantity - result.remaining_quantity
                        total_notional = sum((f.price * f.quantity for f in result.fills), Decimal(0))
                        taker_order.filled_quantity = total_qty
                        taker_order.remaining_quantity = result.remaining_quantity
                        taker_order.notional = total_notional
                        taker_order.avg_price = total_notional / total_qty if total_qty else None
                        taker_order.status = "filled" if result.remaining_quantity == 0 else "partially_filled"
                        await session.commit()
                        for row in updated.values():
                            self.service.remember_ladder_order(self.service._fast_order_snapshot(row, market))
                        await self.runtime.clearinghouse.refresh_contract_market(session, market.id)
                    elif financial_preview:
                        await session.rollback()
                except BaseException:
                    self.service._ladder_financial_unknown[symbol] = order_id
                    await session.rollback()
                    raise
                # Publish mirrors only from completed matcher/confirmed finance facts.
                for fill in internal_fills:
                    maker = before.get(fill.maker_order_id)
                    if maker is None:
                        self.service._ladder_financial_unknown[symbol] = order_id
                        raise ContractValidationError("UNKNOWN internal maker mirror missing")
                    self._fill_snap(maker, fill.quantity, fill.price, now)
                    self.service.remember_ladder_order(maker)
                total_filled = payload.quantity - result.remaining_quantity
                total_notional = sum((f.price * f.quantity for f in result.fills), Decimal(0))
                snap.update(filled_quantity=str(total_filled), remaining_quantity=str(result.remaining_quantity),
                    notional=str(total_notional), avg_price=str(total_notional / total_filled) if total_filled else None,
                    status=("filled" if total_filled and result.remaining_quantity == 0 else "partially_filled" if total_filled and result.placed_on_book else "new" if result.placed_on_book else "canceled"),
                    version=1 if total_filled else 0)
                self.service.remember_ladder_order(snap)
                self.service._fast_ingest_trade_payloads(market, trades, executed_at=now, taker_side=side)
                await self.service._fast_broadcast_contract(market, changed_bids=result.changed_bids, changed_asks=result.changed_asks,
                    trade_payloads=trades, order_snaps=[snap], impacted_users=impacted)
                return {"order": self.service._fast_serialize_order(market, snap), "trades": trades,
                        "state_source": "engine_final", "internal_fill_count": len(internal_fills)}

    @staticmethod
    def _fill_snap(snap, quantity, price, now):
        filled = Decimal(snap["filled_quantity"]) + quantity
        remaining = max(Decimal(0), Decimal(snap["remaining_quantity"]) - quantity)
        notional = Decimal(snap.get("notional") or 0) + price * quantity
        snap.update(filled_quantity=str(filled), remaining_quantity=str(remaining), notional=str(notional),
                    avg_price=str(notional / filled), version=int(snap.get("version") or 0) + 1,
                    status="filled" if remaining == 0 else "partially_filled", updated_at=to_millis(now))

    async def cancel(self, symbol, maker_uid, order_id):
        key = (str(symbol).upper(), int(maker_uid), str(order_id))
        task = self._cancel_inflight.get(key)
        if task is None:
            self.runtime.engine.fault.check()
            task = asyncio.create_task(self._cancel(symbol, maker_uid, order_id), name=f"ladder-cancel-{key[0]}")
            self._cancel_inflight[key] = task
            def finished(done):
                self._cancel_inflight.pop(key, None)
                failed = done.cancelled() or done.exception() is not None
                if failed and key in self._matched_cancels:
                    self.service._ladder_financial_unknown[key[0]] = "cancel failed after matcher: " + key[2]
                    self.runtime.engine.fault.halt("internal cancel result unknown: " + key[2])
                self._matched_cancels.discard(key)
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _cancel(self, symbol, maker_uid, order_id):
        async with self.session_factory() as session:
            market, user = await self._identity(session, symbol, maker_uid)
            async with self.runtime.market_financial_guard(market.symbol, lambda: self._resource_keys(market, maker_uid)):
                self._ready(market.symbol, cancel=True)
                snap = self.service._fast_contract_orders.get(order_id) or self.service._ladder_terminal_orders.get(order_id)
                if snap is None:
                    raise ContractValidationError("order missing; query before retry")
                if int(snap["user_id"]) != int(maker_uid) or int(snap["market_id"]) != int(market.id):
                    raise ContractValidationError("cannot cancel another identity or market")
                if snap["status"] not in {"new", "partially_filled"}:
                    return {"order": self.service._fast_serialize_order(market, snap), "idempotent": True}
                # Only orders with real fill anchors need a SQL cancellation.
                # Acquire its write slot before removing the resting node.
                row = (await session.scalar(select(Order).where(Order.order_id == order_id))) if Decimal(snap.get("filled_quantity") or 0) > 0 else None
                if row is not None:
                    try:
                        await acquire_sqlite_write_admission(session, existing_order_id=order_id)
                        self._ready(market.symbol, cancel=True)
                    except BaseException:
                        await session.rollback()
                        raise
                try:
                    result = await self.runtime.get_symbol_sequencer(market.symbol).submit(CancelOrderCommand(order_id=order_id))
                except BaseException:
                    await session.rollback()
                    raise
                self._matched_cancels.add((market.symbol, int(maker_uid), str(order_id)))
                if result.cancelled_side is None:
                    raise ContractValidationError("UNKNOWN: engine and own order mirror disagree")
                snap = dict(snap)
                snap.update(status="canceled", version=int(snap["version"]) + 1, updated_at=to_millis(datetime.now(tz=UTC)))
                # Already materialized user-fill anchor must not revive on restart.
                if row is not None:
                    row.status = "canceled"
                    row.version = snap["version"]
                    row.updated_at = datetime.now(tz=UTC)
                    row.canceled_at = row.updated_at
                    try:
                        await session.commit()
                    except BaseException:
                        self.service._ladder_financial_unknown[market.symbol] = order_id
                        await session.rollback()
                        raise
                self.service.remember_ladder_order(snap)
                await self.service._fast_broadcast_contract(market, changed_bids=result.changed_bids, changed_asks=result.changed_asks,
                    trade_payloads=[], order_snaps=[snap], impacted_users={int(maker_uid)})
                return {"order": self.service._fast_serialize_order(market, snap), "state_source": "engine_final"}

    async def own_orders(self, symbol, maker_uid):
        async with self.session_factory() as session:
            market, _ = await self._identity(session, symbol, maker_uid)
            async with self.runtime.market_locks[market.symbol]:
                book = self.runtime.engine.ensure_market(market.symbol)
                engine = {oid: o for oid, o in book.orders.items() if int(o.user_id) == int(maker_uid)}
                mirror = {oid: s for oid, s in self.service._fast_contract_orders.items() if int(s["user_id"]) == int(maker_uid) and int(s["market_id"]) == int(market.id) and s["status"] in {"new", "partially_filled"}}
                if set(engine) != set(mirror):
                    raise ContractValidationError("UNKNOWN: complete engine and mirror order sets disagree")
                for oid, order in engine.items():
                    snap = mirror[oid]
                    if order.price != Decimal(snap["price"]) or order.remaining != Decimal(snap["remaining_quantity"]):
                        raise ContractValidationError("UNKNOWN: engine and mirror order content disagree")
                return [self.service._fast_serialize_order(market, s) for s in mirror.values()]

    async def query(self, symbol, maker_uid, *, order_id=None, client_order_id=None):
        symbol = str(symbol).upper()
        if symbol in self.service._ladder_financial_unknown:
            return {"status": "UNKNOWN", "financial_unknown": True, "reason": self.service._ladder_financial_unknown[symbol]}
        key = (symbol, int(maker_uid), str(client_order_id))
        if key in self._inflight:
            return {"status": "ACCEPTED", "pending": True}
        orders = await self.own_orders(symbol, maker_uid)
        for snap in orders + list(self.service._ladder_terminal_orders.values()):
            if snap.get("symbol") == symbol and int(snap.get("user_id", maker_uid)) == int(maker_uid) and ((order_id and snap["order_id"] == order_id) or (client_order_id and snap.get("client_order_id") == client_order_id)):
                return {"order": dict(snap), "state_source": "engine_final"}
        # A recent placement result may be terminal immediately after matching.
        if key in self._requests:
            return deepcopy(self._requests[key])
        return {"status": "NOT_FOUND_FINAL", "pending": False, "state_source": "exclusive_in_process_snapshot"}
