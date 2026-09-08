from __future__ import annotations

import asyncio
import logging
import os
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from app.core.config import settings
from app.core.constants import PRODUCT_TYPE_PERP, PRODUCT_TYPE_SPOT, ROLE_BOT, SIDE_BUY, SIDE_SELL
from app.core.time_utils import to_millis
from app.models.market import Market
from app.models.user import User
from app.schemas.api import (
    ContractOrderAmendRequest,
    ContractOrderBatchAmendItem,
    ContractOrderBatchAmendRequest,
    ContractOrderCreateRequest,
    OrderAmendRequest,
    OrderBatchAmendItem,
    OrderBatchAmendRequest,
    OrderCreateRequest,
)
from app.schemas.quote_set import QuoteLevel, QuoteSetReplaceRequest
from app.services.contract_service import ContractService, ContractValidationError
from app.services.exchange_core import ExchangeCommand, QuoteOperationPlan, QUOTE_SET_REPLACE
from app.services.order_service import OrderService, OrderValidationError
from app.services.persistence_contract import legacy_sampled_runtime, platform_durable_contract
from exchange_common.quote_pipeline import QuoteDiff

logger = logging.getLogger("quote_set_service")

# Temporary detailed diagnostics for the PERP QuoteSet sell-layer topic.
# Each failed operation logs action/side/order_id/desired price/exception stack
# so the failure layer can be attributed without guessing.
QUOTE_SET_OPERATION_DEBUG = str(os.getenv("QUOTE_SET_OPERATION_DEBUG", "0")).strip().lower() in {"1", "true", "yes", "on"}


class QuoteSetService:
    """Adapter from the high-level quote command to the existing fast mirror."""

    def __init__(self, runtime, order_service: OrderService, contract_service: ContractService) -> None:
        self.runtime = runtime
        self.order_service = order_service
        self.contract_service = contract_service
        self._locks: dict[tuple[int, str, str], asyncio.Lock] = {}

    async def submit_spot(self, session, user: User, request: QuoteSetReplaceRequest) -> dict:
        market = await self.order_service._get_market(session, request.symbol)
        if market.product_type != PRODUCT_TYPE_SPOT:
            raise OrderValidationError("quote set symbol is not a SPOT market")
        if not self._writer_ready():
            raise OrderValidationError("quote set journal is unavailable")
        logical_timestamp = self._logical_timestamp(request.logical_timestamp)
        command = self._command(request, user.id, PRODUCT_TYPE_SPOT, logical_timestamp)
        async with self._lock_for(user.id, market.symbol, request.strategy_instance):
            # A sampled bot quote may have been removed by a fill/STP/race
            # while an older fast-mirror entry survived.  Reconcile before the
            # diff so the same generation emits a fresh place instead of an
            # endless amend against an engine-absent order.
            await self.order_service.reconcile_ephemeral_spot_ghosts(user, market)
            current = self._current_orders(self.order_service._fast_orders, user.id, market.id, market.symbol)
            operations = self._build_operations(command, request, current, market, product_type=PRODUCT_TYPE_SPOT)
            return await self._submit(
                session,
                user,
                request,
                command,
                operations,
                market,
                product_type=PRODUCT_TYPE_SPOT,
            )

    async def submit_contract(self, session, user: User, request: QuoteSetReplaceRequest) -> dict:
        market = await self.contract_service.get_market(session, request.symbol)
        if market.product_type != PRODUCT_TYPE_PERP:
            raise ContractValidationError("quote set symbol is not a PERP market")
        if not self._writer_ready():
            raise ContractValidationError("quote set journal is unavailable")
        logical_timestamp = self._logical_timestamp(request.logical_timestamp)
        command = self._command(request, user.id, PRODUCT_TYPE_PERP, logical_timestamp)
        async with self._lock_for(user.id, market.symbol, request.strategy_instance):
            current = self._current_orders(self.contract_service._fast_contract_orders, user.id, market.id, market.symbol)
            operations = self._build_operations(command, request, current, market, product_type=PRODUCT_TYPE_PERP)
            if (
                platform_durable_contract()
                or (settings.persistence_mode == "memory" and str(user.role) == ROLE_BOT)
            ):
                operations = self._ephemeral_contract_close_release_first(operations)
            return await self._submit(
                session,
                user,
                request,
                command,
                operations,
                market,
                product_type=PRODUCT_TYPE_PERP,
            )

    def _lock_for(self, user_id: int, symbol: str, strategy_instance: str) -> asyncio.Lock:
        key = (int(user_id), symbol.upper(), strategy_instance)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _writer_ready(self) -> bool:
        writer = getattr(self.runtime, "persistence_writer", None)
        return (
            writer is not None
            and not writer._stopping
            and writer._blocked_task is None
            and getattr(writer, "_critical_blocked", None) is None
        )

    @staticmethod
    def _logical_timestamp(value: int | datetime | None) -> int:
        if isinstance(value, datetime):
            return to_millis(value if value.tzinfo is not None else value.replace(tzinfo=UTC))
        if value is not None:
            return int(value)
        # Time is an ingress concern.  ExchangeCore itself never reads it.
        return to_millis(datetime.now(tz=UTC))

    @staticmethod
    def _command(request: QuoteSetReplaceRequest, user_id: int, product_type: str, logical_timestamp: int) -> ExchangeCommand:
        payload = request.canonical_payload()
        return ExchangeCommand(
            command_id=request.command_id,
            command_type=QUOTE_SET_REPLACE,
            symbol=request.symbol,
            account_id=user_id,
            product_type=product_type,
            payload=payload,
            logical_timestamp=logical_timestamp,
            strategy_instance=request.strategy_instance,
            generation=request.generation,
            config_version=request.config_version,
        )

    @staticmethod
    def _current_orders(source: dict[str, dict], user_id: int, market_id: int, symbol: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for snap in source.values():
            if int(snap.get("user_id") or -1) != int(user_id):
                continue
            if int(snap.get("market_id") or -1) != int(market_id):
                continue
            if str(snap.get("symbol") or "").upper() != symbol.upper():
                continue
            if str(snap.get("status")) not in {"new", "partially_filled"}:
                continue
            client_order_id = str(snap.get("client_order_id") or "")
            if not client_order_id:
                continue
            result[f"{snap.get('side')}:{client_order_id}"] = dict(snap)
        return result

    def _build_operations(
        self,
        command: ExchangeCommand,
        request: QuoteSetReplaceRequest,
        current: dict[str, dict[str, Any]],
        market: Market,
        *,
        product_type: str,
    ) -> list[dict[str, Any]]:
        core = self.runtime.exchange_core
        # Keep a product-neutral diff beside the product-specific reserve
        # builder.  Spot and Perp may validate different risk fields, but the
        # identity/noop/amend/place boundary is the same client-order patch.
        shared_current = [
            {
                "side": snap.get("side"),
                "price": snap.get("price"),
                "quantity": snap.get("remaining_quantity", snap.get("quantity", "0")),
                "client_order_id": key.split(":", 1)[-1],
                "position_action": snap.get("position_action", "open"),
                "reduce_only": snap.get("reduce_only", False),
                "leverage": snap.get("leverage"),
            }
            for key, snap in current.items()
        ]
        shared_desired = [
            {
                "side": side,
                "price": level.price,
                "quantity": level.quantity,
                "client_order_id": self._level_identity(level, side, index),
                "position_action": getattr(level, "position_action", "open"),
                "reduce_only": bool(getattr(level, "reduce_only", False)),
                "leverage": getattr(level, "leverage", None),
            }
            for side, levels in ((SIDE_BUY, request.bids), (SIDE_SELL, request.asks))
            for index, level in enumerate(levels, start=1)
        ]
        shared_diff = QuoteDiff.between(shared_current, shared_desired)
        plan = core.expand_quote_set(
            command,
            current_orders=current,
            sequence=core.next_sequence_hint(),
        )
        by_key = {f"{side}:{self._level_identity(level, side, index)}": level for side, levels in ((SIDE_BUY, request.bids), (SIDE_SELL, request.asks)) for index, level in enumerate(levels, start=1)}
        now = datetime.fromtimestamp(command.logical_timestamp / 1000, tz=UTC).isoformat()
        operations = QuoteOperationPlan()
        for operation in plan:
            action = str(operation.get("action"))
            key = str(operation.get("key") or "")
            desired = dict(operation.get("desired") or {})
            level = by_key.get(key)
            if level is None and action != "cancel":
                logger.warning(
                    "quote_set build dropped operation key=%s action=%s side=%s desired_client=%s",
                    key,
                    action,
                    desired.get("side"),
                    desired.get("client_order_id"),
                )
                continue
            current_order = current.get(key) or {}
            if action == "noop":
                operations.append(operation)
                continue
            if action == "cancel":
                operations.append(
                    {
                        **operation,
                        "side": current_order.get("side"),
                        "now": now,
                        "remaining_quantity": current_order.get("remaining_quantity", current_order.get("quantity", "0")),
                    }
                )
                continue
            assert level is not None
            quantity = Decimal(str(level.quantity))
            price = Decimal(str(level.price))
            if product_type == PRODUCT_TYPE_SPOT:
                payload = self._spot_payload(request.symbol, level, desired["side"], price, quantity)
                reserve = {
                    "asset": market.quote_asset if desired["side"] == SIDE_BUY else market.base_asset,
                    "amount": str(price * quantity if desired["side"] == SIDE_BUY else quantity),
                }
            else:
                payload = self._contract_payload(request.symbol, level, desired["side"], price, quantity)
                leverage = Decimal(str(level.leverage or market.default_leverage))
                reserve = {
                    "amount": str(price * quantity / leverage if level.position_action == "open" else Decimal("0")),
                    "leverage": str(leverage),
                }
            payload["client_order_id"] = str(desired["client_order_id"])
            if action == "amend":
                filled = Decimal(str(current_order.get("filled_quantity") or "0"))
                payload["quantity"] = str(filled + quantity)
                order_id = str(current_order.get("order_id") or operation.get("order_id"))
            else:
                order_id = str(operation.get("order_id"))
            engine_result = {
                "remaining_quantity": str(quantity),
                "placed_on_book": True,
                "stop_reason": None,
                "fills": [],
                "changed_bids": [],
                "changed_asks": [],
            }
            operations.append(
                {
                    **operation,
                    "order_id": order_id,
                    "now": now,
                    "payload": payload,
                    "reserve": reserve,
                    "engine_result": engine_result,
                    "new_remaining": str(quantity),
                    "old_remaining": str(
                        current_order.get("remaining_quantity", current_order.get("quantity", "0"))
                    ),
                    "amend_kind": "simple",
                }
            )
        logger.debug(
            "quote_set build summary symbol=%s plan=%s built=%s by_key=%s",
            request.symbol,
            len(plan),
            len(operations),
            len(by_key),
        )
        operations.desired_levels = int(getattr(plan, "desired_levels", len(by_key)))
        operations.noop_elided = int(getattr(plan, "noop_elided", 0))
        operations.bytes_before = int(getattr(plan, "bytes_before", 0))
        operations.bytes_after = len(str(operations).encode("utf-8"))
        operations.shared_diff_changed = shared_diff.changed_count
        operations.shared_diff_noop = shared_diff.noop_count
        return operations

    @staticmethod
    def _ephemeral_contract_close_release_first(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Release stale robot CLOSE reservations before consuming capacity.

        FLOW can reduce a live position between quote generations while the
        old reduce-only levels still reserve the previous size.  Stable sorting
        strict CLOSE shrinks ahead of places/increases lets those safe amends
        heal the mirror before later operations are validated.  This ordering
        is deliberately enabled only by the memory+mm_bot caller.
        """

        def priority(operation: dict[str, Any]) -> int:
            action = str(operation.get("action") or "")
            if action == "cancel":
                return 0
            payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
            is_close = (
                str(payload.get("position_action") or "").lower() == "close"
                and bool(payload.get("reduce_only"))
            )
            if action == "amend" and is_close:
                old_remaining = Decimal(str(operation.get("old_remaining") or "0"))
                new_remaining = Decimal(str(operation.get("new_remaining") or "0"))
                if new_remaining < old_remaining:
                    return 1
            if action == "noop":
                return 2
            return 3

        return sorted(operations, key=priority)

    @staticmethod
    def _level_identity(level: QuoteLevel, side: str, index: int) -> str:
        return str(level.client_order_id or level.tag or f"{side}-{index:03d}")

    @staticmethod
    def _spot_payload(symbol: str, level: QuoteLevel, side: str, price: Decimal, quantity: Decimal) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "tif": "gtc",
            "quantity": str(quantity),
            "price": str(price),
            "client_order_id": level.client_order_id or level.tag,
        }

    @staticmethod
    def _contract_payload(symbol: str, level: QuoteLevel, side: str, price: Decimal, quantity: Decimal) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "tif": "gtc",
            "quantity": str(quantity),
            "price": str(price),
            "position_action": level.position_action,
            "reduce_only": bool(level.reduce_only),
            "client_order_id": level.client_order_id or level.tag,
        }

    async def _submit(
        self,
        session,
        user: User,
        request: QuoteSetReplaceRequest,
        command: ExchangeCommand,
        operations: list[dict[str, Any]],
        market: Market,
        *,
        product_type: str,
    ) -> dict:
        writer = self.runtime.persistence_writer
        service = self.order_service if product_type == PRODUCT_TYPE_SPOT else self.contract_service
        quote_validation_context = (
            await self.contract_service.quote_validation_context(session, user, market)
            if product_type == PRODUCT_TYPE_PERP
            else None
        )
        ephemeral_robot_quote_set = (
            (platform_durable_contract() and str(user.role) == ROLE_BOT)
            or (settings.persistence_mode == "memory" and str(user.role) == ROLE_BOT)
        )

        # Fill side after the common operation builder has retained the level
        # identity.  This keeps schema and journal payloads explicit.
        for operation in operations:
            desired = operation.get("desired") or {}
            if operation.get("action") in {"place", "amend"}:
                operation["payload"]["side"] = desired.get("side")
            elif operation.get("action") == "cancel":
                if operation.get("side") not in {SIDE_BUY, SIDE_SELL}:
                    raise OrderValidationError("quote set cancel operation is missing side")

        async def durable(record: dict) -> dict:
            if ephemeral_robot_quote_set:
                # The default sandbox contract explicitly keeps robot quote
                # animation in memory.  Journaling a 100-level QuoteSet and then
                # materializing every amend creates ledger/accounting/outbox
                # fan-out even though those quotes are restart-ephemeral.
                writer.record_ephemeral_quote_set(len(operations))
                # This command envelope is intentionally not durable.  Preserve
                # the last actual durable watermark instead of claiming the new
                # exchange sequence was committed.
                return {"durable_seq": writer.durable_sequence()}
            return await writer.append_command(record)

        async def execute(_command: ExchangeCommand, sequence: int, plan: list[dict[str, Any]]) -> dict:
            accepted_count = 0
            rejected_count = 0
            noop_count = int(getattr(plan, "noop_elided", 0))
            operation_counts: Counter[str] = Counter()
            failure_samples: list[str] = []
            failure_categories: Counter[str] = Counter()
            first_error: str | None = None
            flows: list[dict] = []
            # The published BBO is immutable for the duration of one quote
            # patch.  Reusing it for the crossing guard removes one REST-style
            # snapshot lookup per amend while preserving user interaction:
            # the matching engine still evaluates the live book under its
            # final STP/crossing boundary for every operation.
            bbo_snapshot, _, _ = self.runtime.orderbook_snapshot_unlocked(_command.symbol, depth=1)
            logger.debug(
                "quote_set execute start symbol=%s sequence=%s plan_len=%s",
                _command.symbol,
                sequence,
                len(plan),
            )
            atomic_unified_quote = (
                ephemeral_robot_quote_set
                and str(settings.core_mode or "legacy").lower() == "unified"
            )
            operation_context = (
                writer.capture_operations()
                if atomic_unified_quote or not ephemeral_robot_quote_set
                else writer.ephemeral_quote_set_operations()
            )
            captured_operations: list[dict[str, Any]] = []

            async def apply_one(operation: dict[str, Any]) -> None:
                nonlocal accepted_count, rejected_count, first_error, noop_count
                action = str(operation.get("action"))
                if action == "noop":
                    noop_count += 1
                    return
                operation_counts[action] += 1
                try:
                    if product_type == PRODUCT_TYPE_SPOT:
                        result = await self._apply_spot_operation(
                            session,
                            user,
                            operation,
                            allow_client_order_reuse=ephemeral_robot_quote_set,
                            market=market,
                            bbo_snapshot=bbo_snapshot,
                        )
                    else:
                        result = await self._apply_contract_operation(
                            session,
                            user,
                            operation,
                            allow_client_order_reuse=ephemeral_robot_quote_set,
                            market=market,
                            bbo_snapshot=bbo_snapshot,
                            quote_validation_context=quote_validation_context,
                        )
                    if result is None:
                        raise RuntimeError("fast order path unavailable")
                    self._assert_engine_result_accepted(result, action)
                    accepted_count += 1
                    flow = result.get("_fast_flow")
                    if isinstance(flow, dict):
                        flows.append(flow)
                except (OrderValidationError, ContractValidationError, ValueError, RuntimeError) as exc:
                    rejected_count += 1
                    error_text = str(exc)[:256]
                    category = self._classify_failure(error_text)
                    failure_categories[category] += 1
                    if first_error is None:
                        first_error = error_text
                    if len(failure_samples) < 8:
                        failure_samples.append(
                            f"{action}/{operation.get('side') or (operation.get('desired') or {}).get('side')}/"
                            f"{str(operation.get('order_id'))[:48]}:{error_text}"
                        )
                    if QUOTE_SET_OPERATION_DEBUG:
                        logger.warning(
                            "quote_set operation failed action=%s side=%s order_id=%s error=%s",
                            action,
                            operation.get("side") or (operation.get("desired") or {}).get("side"),
                            operation.get("order_id"),
                            error_text,
                        )

            async def apply_amend_batch(batch_ops: list[dict[str, Any]]) -> bool:
                """Use one symbol-sequencer batch for ordinary non-crossing amends.

                A batch endpoint explicitly rejects a target that crosses the
                published BBO. Those entries are immediately replayed through
                ``apply_one`` so a user order remains a valid counterparty and
                the engine-level crossing/STP boundary is still final.
                """
                nonlocal accepted_count
                if len(batch_ops) < 2:
                    return False
                try:
                    if product_type == PRODUCT_TYPE_SPOT:
                        batch_payload = OrderBatchAmendRequest(
                            symbol=_command.symbol,
                            orders=[
                                OrderBatchAmendItem(
                                    order_id=str(operation["order_id"]),
                                    quantity=Decimal(str(operation["payload"]["quantity"])),
                                    price=Decimal(str(operation["payload"]["price"])),
                                )
                                for operation in batch_ops
                            ],
                        )
                        result = await self.order_service.amend_order_batch_fast(
                            session,
                            user,
                            batch_payload,
                            broadcast=False,
                            market_override=market,
                            bbo_snapshot=bbo_snapshot,
                            include_orders=False,
                        )
                    else:
                        batch_payload = ContractOrderBatchAmendRequest(
                            symbol=_command.symbol,
                            orders=[
                                ContractOrderBatchAmendItem(
                                    order_id=str(operation["order_id"]),
                                    quantity=Decimal(str(operation["payload"]["quantity"])),
                                    price=Decimal(str(operation["payload"]["price"])),
                                )
                                for operation in batch_ops
                            ],
                        )
                        result = await self.contract_service.amend_order_batch_fast(
                            session,
                            user,
                            batch_payload,
                            broadcast=False,
                            market_override=market,
                            bbo_snapshot=bbo_snapshot,
                            include_orders=False,
                        )
                except (OrderValidationError, ContractValidationError, ValueError, RuntimeError):
                    return False
                if not isinstance(result, dict):
                    return False
                success_indices = {
                    int(item.get("index"))
                    for item in result.get("items") or []
                    if isinstance(item, dict) and item.get("index") is not None
                }
                failed_indices = {
                    int(item.get("index"))
                    for item in result.get("failed") or []
                    if isinstance(item, dict) and item.get("index") is not None
                }
                if not success_indices and not failed_indices:
                    return False
                operation_counts["amend"] += len(success_indices)
                accepted_count += len(success_indices)
                flow = result.get("_fast_flow")
                if isinstance(flow, dict):
                    flows.append(flow)
                for index, operation in enumerate(batch_ops):
                    if index in success_indices:
                        continue
                    # Crossing, stale, risk and mirror failures retain the
                    # normal per-order path; user interaction is never lost
                    # merely because the fast batch path declined an item.
                    await apply_one(operation)
                return True

            # ExchangeCore is already the authoritative market worker for a
            # QuoteSet.  The compatibility SymbolSequencer remains available
            # for direct REST commands, but must execute inline here so one
            # QuoteSet cannot wait on a second queue/future timeline.
            with self.runtime.get_symbol_sequencer(_command.symbol).inline_execution():
                with operation_context:
                    index = 0
                    while index < len(plan):
                        operation = plan[index]
                        if str(operation.get("action")) == "amend":
                            end = index + 1
                            while end < len(plan) and str(plan[end].get("action")) == "amend":
                                end += 1
                            batch_ops = list(plan[index:end])
                            if await apply_amend_batch(batch_ops):
                                index = end
                                continue
                        await apply_one(operation)
                        index += 1
                    if atomic_unified_quote and writer.is_capturing():
                        # The context manager owns the actual list; copy it
                        # before it resets after all levels complete.
                        captured_operations = list(writer._capture_var.get() or [])
            if atomic_unified_quote:
                durable_tail = [
                    task
                    for task in captured_operations
                    if not writer._safe_ephemeral_quote_amend(
                        task,
                        task.get("engine_result") if isinstance(task.get("engine_result"), dict) else {},
                    )
                ]
                if durable_tail:
                    await writer.enqueue_durable_batch(durable_tail)
            if product_type == PRODUCT_TYPE_SPOT:
                changed_bids = [item for flow in flows for item in flow.get("changed_bids", [])]
                changed_asks = [item for flow in flows for item in flow.get("changed_asks", [])]
                trades = [item for flow in flows for item in flow.get("trade_payloads", [])]
                if changed_bids or changed_asks or trades:
                    await self.order_service._fast_broadcast(
                        market,
                        changed_bids=changed_bids,
                        changed_asks=changed_asks,
                        trade_payloads=trades,
                    )
            else:
                changed_bids = [item for flow in flows for item in flow.get("changed_bids", [])]
                changed_asks = [item for flow in flows for item in flow.get("changed_asks", [])]
                trades = [item for flow in flows for item in flow.get("trade_payloads", [])]
                snaps = [item for flow in flows for item in flow.get("order_snaps", [])]
                impacted = {uid for flow in flows for uid in flow.get("impacted_users", set())}
                if changed_bids or changed_asks or trades or snaps:
                    await self.contract_service._fast_broadcast_contract(
                        market,
                        changed_bids=changed_bids,
                        changed_asks=changed_asks,
                        trade_payloads=trades,
                        order_snaps=snaps,
                        impacted_users=impacted or {int(user.id)},
                    )
            if accepted_count or rejected_count or noop_count:
                writer.mark_matched(sequence)
                writer.mark_published(sequence)
            logger.debug(
                "quote_set execute completed symbol=%s plan_len=%s accepted=%s failed=%s",
                _command.symbol,
                len(plan),
                accepted_count,
                rejected_count,
            )
            return {
                "quote_set": True,
                "requested_count": len(plan),
                "desired_levels": int(getattr(plan, "desired_levels", len(plan))),
                "accepted_count": accepted_count,
                "rejected_count": rejected_count,
                "failed_count": rejected_count,
                "changed_count": accepted_count,
                "noop_count": noop_count,
                "first_error": first_error,
                "operation_counts": dict(operation_counts),
                "failed_samples": failure_samples,
                "failure_categories": dict(failure_categories),
                "journal_commands": 0 if ephemeral_robot_quote_set else 1,
                "ephemeral_quote_set": ephemeral_robot_quote_set,
            }

        ack = await self.runtime.exchange_core.submit_quote_set(
            command,
            operation_plan=operations,
            before_execute=durable,
            execute=execute,
        )
        logger.debug(
            "quote_set submit account=%s symbol=%s generation=%s status=%s requested=%s accepted=%s failed=%s samples=%s",
            user.username,
            request.symbol,
            request.generation,
            ack.status,
            len(operations),
            ack.accepted_count,
            ack.rejected_count,
            list(ack.failure_samples),
        )
        response = ack.as_dict()
        response["quote_set"] = True
        response["desired_levels"] = int(getattr(operations, "desired_levels", len(request.bids) + len(request.asks)))
        response["requested_count"] = len(operations)
        response["journal_commands"] = 0 if ephemeral_robot_quote_set else 1
        response["ephemeral_quote_set"] = ephemeral_robot_quote_set
        response["strategy_instance"] = request.strategy_instance
        response["generation"] = request.generation
        response["durability_mode"] = (
            "memory_ephemeral"
            if ephemeral_robot_quote_set
            else writer.metrics_snapshot().get("durability_mode")
        )
        return response

    @staticmethod
    def _assert_engine_result_accepted(result: dict[str, Any], action: str) -> None:
        engine_result = result.get("engine_result") if isinstance(result, dict) else None
        if not isinstance(engine_result, dict):
            return
        stp_action = str(engine_result.get("stp_action") or "")
        if stp_action in {"cancel_taker", "reject"} and not bool(engine_result.get("placed_on_book")):
            raise OrderValidationError(f"engine rejected {action}: stp/{stp_action}")

    @staticmethod
    def _classify_failure(error_text: str) -> str:
        text = str(error_text).lower()
        if "stale" in text or "generation" in text:
            return "stale"
        if "stp" in text or "self trade" in text or "self-trade" in text:
            return "stp"
        if any(term in text for term in ("insufficient", "balance", "margin")):
            return "insufficient_balance"
        if any(term in text for term in ("position", "reduce_only", "reduce-only", "one-way")):
            return "position_conflict"
        if any(term in text for term in ("mirror", "engine amend", "engine place", "order not found")):
            return "mirror_mismatch"
        if any(term in text for term in ("writer", "journal", "storage", "persistence")):
            return "writer_storage_degraded"
        return "validation_or_unknown"

    async def _apply_spot_operation(
        self,
        session,
        user: User,
        operation: dict,
        *,
        allow_client_order_reuse: bool = False,
        market: Market | None = None,
        bbo_snapshot: dict[str, list[list[str]]] | None = None,
    ) -> dict | None:
        action = str(operation.get("action"))
        order_id = operation.get("order_id")
        if action == "place":
            payload = OrderCreateRequest.model_validate(operation["payload"])
            return await self.order_service.place_order_fast(
                session,
                user,
                payload,
                broadcast=False,
                now=datetime.fromisoformat(operation["now"]),
                order_id=str(order_id),
                command_sequence=int(operation.get("sequence_number") or 0),
                allow_client_order_reuse=allow_client_order_reuse or bool(operation.get("replaces_order")),
                market_override=market,
                quote_fast_path=allow_client_order_reuse and str(user.role) == ROLE_BOT,
            )
        if action == "amend":
            payload = OrderAmendRequest.model_validate(operation["payload"])
            return await self.order_service.amend_order_fast(
                session,
                user,
                str(order_id),
                payload,
                broadcast=False,
                now=datetime.fromisoformat(operation["now"]),
                command_sequence=int(operation.get("sequence_number") or 0),
                market_override=market,
                bbo_snapshot=bbo_snapshot,
            )
        if action == "cancel":
            result = await self.order_service.cancel_order_fast(
                session,
                user,
                str(order_id),
                broadcast=False,
                now=datetime.fromisoformat(operation["now"]),
                command_sequence=int(operation.get("sequence_number") or 0),
            )
            if result is None:
                raise RuntimeError("fast order path unavailable")
            return result
        return None

    async def _apply_contract_operation(
        self,
        session,
        user: User,
        operation: dict,
        *,
        allow_client_order_reuse: bool = False,
        market: Market | None = None,
        bbo_snapshot: dict[str, list[list[str]]] | None = None,
        quote_validation_context: dict | None = None,
    ) -> dict | None:
        action = str(operation.get("action"))
        order_id = operation.get("order_id")
        side = str(operation.get("side") or (operation.get("desired") or {}).get("side") or "?")
        client_order_id = (operation.get("desired") or {}).get("client_order_id")
        desired_price = (operation.get("desired") or {}).get("price")
        try:
            if action == "place":
                payload = ContractOrderCreateRequest.model_validate(operation["payload"])
                result = await self.contract_service.place_order_fast(
                        session,
                        user,
                        payload,
                        broadcast=False,
                        now=datetime.fromisoformat(operation["now"]),
                        order_id=str(order_id),
                        command_sequence=int(operation.get("sequence_number") or 0),
                        allow_client_order_reuse=allow_client_order_reuse or bool(operation.get("replaces_order")),
                        market_override=market,
                        quote_validation_context=quote_validation_context,
                    )
            elif action == "amend":
                payload = ContractOrderAmendRequest.model_validate(operation["payload"])
                result = await self.contract_service.amend_order_fast(
                        session,
                        user,
                        str(order_id),
                        payload,
                        broadcast=False,
                    now=datetime.fromisoformat(operation["now"]),
                    command_sequence=int(operation.get("sequence_number") or 0),
                    market_override=market,
                    bbo_snapshot=bbo_snapshot,
                )
            elif action == "cancel":
                result = await self.contract_service.cancel_order_fast(
                    session,
                    user,
                    str(order_id),
                    broadcast=False,
                    now=datetime.fromisoformat(operation["now"]),
                    command_sequence=int(operation.get("sequence_number") or 0),
                )
                if result is None:
                    raise RuntimeError("fast order path unavailable")
                return result
            else:
                return None
        except Exception as exc:
            if QUOTE_SET_OPERATION_DEBUG:
                logger.warning(
                    "quote_set contract operation raised action=%s side=%s order_id=%s client_order_id=%s "
                    "desired_price=%s error=%s",
                    action,
                    side,
                    order_id,
                    client_order_id,
                    desired_price,
                    exc,
                    exc_info=True,
                )
            raise
        if result is None:
            reason = self._contract_fast_none_reason(action, order_id)
            message = f"fast order {action} unavailable: {reason}"
            if QUOTE_SET_OPERATION_DEBUG:
                logger.warning(
                    "quote_set contract operation returned None action=%s side=%s order_id=%s "
                    "client_order_id=%s desired_price=%s reason=%s",
                    action,
                    side,
                    order_id,
                    client_order_id,
                    desired_price,
                    reason,
                )
            raise ContractValidationError(message)
        return result

    def _contract_fast_none_reason(self, action: str, order_id: object) -> str:
        service = self.contract_service
        writer = getattr(self.runtime, "persistence_writer", None)
        writer_ready = (
            writer is not None
            and not writer._stopping
            and writer._blocked_task is None
            and getattr(writer, "_critical_blocked", None) is None
        )
        if not writer_ready:
            blocked = None
            if writer is not None:
                blocked = writer._critical_blocked or writer._blocked_task
            if blocked:
                return f"persistence writer blocked ({blocked.get('kind') or 'unknown'}: {str(blocked.get('error'))[:80]})"
            return "persistence writer not ready"
        if str(order_id) not in service._fast_contract_orders:
            return "order not found on fast mirror"
        if action == "amend":
            return "fast amend rejected by engine or crossing settlement (see contract_service log)"
        if action == "place":
            return "fast place rejected by engine or settlement (see contract_service log)"
        return "fast cancel rejected by engine (order not on book)"
