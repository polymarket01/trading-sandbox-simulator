from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.services.matching_faults import BusinessRejected, NotExecuted, BookInvariantError
from app.services.execution_events import EventOutbox, ExecutionEvent, decimal_payload
from app.services.matching_engine import (
    AmendOrderRequest,
    BatchAmendResult,
    BulkQuotePatchOperation,
    BulkQuotePatchResult,
    EngineResult,
    MatchingEngine,
)
from exchange_common.quote_pipeline import SelfTradePolicy


@dataclass(slots=True)
class NewOrderCommand:
    order_id: str
    user_id: int
    side: str
    quantity: Decimal
    created_at: datetime
    limit_price: Decimal | None = None
    can_rest: bool = False
    max_price: Decimal | None = None
    min_price: Decimal | None = None
    maker_guard: Callable[[str, int], bool] | None = None
    sequence_number: int | None = None
    stp_account_key: str | None = None
    stp_group_key: str | None = None
    stp_is_bot: bool = False
    stp_mode: str = "cancel_taker"
    stp_policy: SelfTradePolicy | None = None


@dataclass(slots=True)
class CancelOrderCommand:
    order_id: str
    sequence_number: int | None = None


@dataclass(slots=True)
class AmendOrderCommand:
    order_id: str
    new_price: Decimal
    new_remaining: Decimal
    sequence_number: int | None = None


@dataclass(slots=True)
class CrossingAmendCommand:
    order_id: str
    user_id: int
    side: str
    new_price: Decimal
    new_remaining: Decimal
    created_at: datetime
    sequence_number: int | None = None
    stp_account_key: str | None = None
    stp_group_key: str | None = None
    stp_is_bot: bool = False
    stp_mode: str = "cancel_taker"
    stp_policy: SelfTradePolicy | None = None


@dataclass(slots=True)
class BatchAmendItem:
    order_id: str
    new_price: Decimal
    new_remaining: Decimal


@dataclass(slots=True)
class BatchAmendCommand:
    items: list[BatchAmendItem]


@dataclass(slots=True)
class BulkQuotePatchCommand:
    """Single-writer command for an atomic quote patch on one market."""

    operations: list[BulkQuotePatchOperation]


@dataclass(slots=True)
class PreviewBboOrderCommand:
    side: str
    quantity: Decimal
    limit_price: Decimal
    maker_guard: Callable[[str, int, Decimal, Decimal], bool] | None = None
    stp_account_key: str | None = None
    stp_is_bot: bool = False
    stp_policy: SelfTradePolicy | None = None


SequencerCommand = (
    NewOrderCommand
    | CancelOrderCommand
    | AmendOrderCommand
    | CrossingAmendCommand
    | BatchAmendCommand
    | BulkQuotePatchCommand
    | PreviewBboOrderCommand
)


@dataclass(slots=True)
class SequencerResult:
    sequence_number: int
    engine_result: EngineResult | None = None
    amend_result: object | None = None
    batch_amend_result: BatchAmendResult | None = None
    bulk_quote_patch_result: BulkQuotePatchResult | None = None
    cancelled_side: str | None = None
    cancelled_remaining: Decimal | None = None
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class _QueuedCommand:
    command: SequencerCommand
    future: asyncio.Future[SequencerResult]


class SymbolSequencer:
    def __init__(
        self,
        *,
        symbol: str,
        engine: MatchingEngine,
        outbox: EventOutbox,
        next_sequence: Callable[[str], int],
        observe_sequence: Callable[[str, int | None], None] | None = None,
        max_queue: int = 4096,
    ) -> None:
        self.symbol = symbol.upper()
        self.engine = engine
        self.fault = engine.fault
        engine.sequencers.add(self)
        self.outbox = outbox
        self.next_sequence = next_sequence
        self.observe_sequence = observe_sequence
        self._queue: asyncio.Queue[_QueuedCommand] = asyncio.Queue(maxsize=max(1, int(max_queue)))
        self._worker_task: asyncio.Task | None = None
        self._stopping = False
        self._inline_depth = 0
        self._inline_owner = None

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._stopping = False
            self._worker_task = asyncio.create_task(self._run(), name=f"sequencer-{self.symbol}")

    async def stop(self) -> None:
        self._stopping = True
        if self._worker_task is not None:
            self._worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker_task
        self._worker_task = None
        while not self._queue.empty():
            queued = self._queue.get_nowait()
            if not queued.future.done():
                queued.future.set_exception(NotExecuted("sequencer stopped before execution"))
            self._queue.task_done()

    async def submit(self, command: SequencerCommand) -> SequencerResult:
        self.fault.check()
        if self._inline_depth > 0 and self._inline_owner is asyncio.current_task():
            # ExchangeCore's market worker already owns the execution turn.
            # Inline execution prevents a second queue/future timeline for a
            # QuoteSet while preserving the legacy queue for direct callers.
            return self._execute(command)
        self.start()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[SequencerResult] = loop.create_future()
        try:
            self._queue.put_nowait(_QueuedCommand(command=command, future=future))
        except asyncio.QueueFull as exc:
            raise NotExecuted(f"symbol sequencer queue is full: {self.symbol}") from exc
        # Retrieve errors even when all request waiters have disconnected.
        future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(future)

    @contextlib.contextmanager
    def inline_execution(self):
        owner = asyncio.current_task()
        if self._inline_owner is not None and self._inline_owner is not owner:
            raise NotExecuted("another task owns this symbol inline context")
        self._inline_owner = owner
        self._inline_depth += 1
        try:
            yield
        finally:
            self._inline_depth -= 1
            if self._inline_depth == 0:
                self._inline_owner = None

    async def _run(self) -> None:
        while not self._stopping:
            queued = await self._queue.get()
            try:
                result = self._execute(queued.command)
            except Exception as exc:
                if not isinstance(exc, BusinessRejected):
                    self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
                if not queued.future.done():
                    queued.future.set_exception(exc)
            else:
                if not queued.future.done():
                    queued.future.set_result(result)
            finally:
                self._queue.task_done()

    def _execute(self, command: SequencerCommand) -> SequencerResult:
        self.fault.check()
        try:
            return self._dispatch(command)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def _dispatch(self, command: SequencerCommand) -> SequencerResult:
        if isinstance(command, NewOrderCommand):
            return self._execute_new(command)
        if isinstance(command, CancelOrderCommand):
            return self._execute_cancel(command)
        if isinstance(command, AmendOrderCommand):
            return self._execute_amend(command)
        if isinstance(command, CrossingAmendCommand):
            return self._execute_crossing_amend(command)
        if isinstance(command, BatchAmendCommand):
            return self._execute_batch_amend(command)
        if isinstance(command, BulkQuotePatchCommand):
            return self._execute_bulk_quote_patch(command)
        if isinstance(command, PreviewBboOrderCommand):
            return self._execute_preview_bbo(command)
        raise TypeError(f"unsupported command: {type(command)!r}")

    def _execute_preview_bbo(self, command: PreviewBboOrderCommand) -> SequencerResult:
        # Read-only admission preview: no sequence allocation and no outbox event.
        result = self.engine.preview_bbo_order(
            self.symbol,
            side=command.side,
            quantity=command.quantity,
            limit_price=command.limit_price,
            maker_guard=command.maker_guard,
            taker_account_key=command.stp_account_key,
            taker_is_bot=command.stp_is_bot,
            stp_policy=command.stp_policy,
        )
        return SequencerResult(sequence_number=0, engine_result=result)

    def _execute_new(self, command: NewOrderCommand) -> SequencerResult:
        sequence_number = command.sequence_number or self.next_sequence(self.symbol)
        if command.sequence_number is not None and self.observe_sequence is not None:
            self.observe_sequence(self.symbol, sequence_number)
        result = self.engine.process_order(
            symbol=self.symbol,
            order_id=command.order_id,
            user_id=command.user_id,
            side=command.side,
            quantity=command.quantity,
            created_at=command.created_at,
            limit_price=command.limit_price,
            can_rest=command.can_rest,
            max_price=command.max_price,
            min_price=command.min_price,
            sequence_number=sequence_number,
            maker_guard=command.maker_guard,
            stp_account_key=command.stp_account_key,
            stp_group_key=command.stp_group_key,
            stp_is_bot=command.stp_is_bot,
            stp_mode=command.stp_mode,
            stp_policy=command.stp_policy,
        )
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="order_accepted",
                symbol=self.symbol,
                sequence_number=sequence_number,
                order_id=command.order_id,
                payload={
                    "side": command.side,
                    "quantity": decimal_payload(command.quantity),
                    "limit_price": decimal_payload(command.limit_price),
                    "placed_on_book": result.placed_on_book,
                },
            )
        )
        for fill in result.fills:
            self.outbox.publish_nowait(
                ExecutionEvent(
                    event_type="order_matched",
                    symbol=self.symbol,
                    sequence_number=sequence_number,
                    order_id=command.order_id,
                    payload={
                        "maker_order_id": fill.maker_order_id,
                        "maker_user_id": fill.maker_user_id,
                        "price": decimal_payload(fill.price),
                        "quantity": decimal_payload(fill.quantity),
                    },
                )
            )
        return SequencerResult(sequence_number=sequence_number, engine_result=result)

    def _execute_cancel(self, command: CancelOrderCommand) -> SequencerResult:
        sequence_number = command.sequence_number or self.next_sequence(self.symbol)
        if command.sequence_number is not None and self.observe_sequence is not None:
            self.observe_sequence(self.symbol, sequence_number)
        side, remaining, changes = self.engine.cancel_order(self.symbol, command.order_id)
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="order_cancelled",
                symbol=self.symbol,
                sequence_number=sequence_number,
                order_id=command.order_id,
                payload={"side": side, "remaining": decimal_payload(remaining)},
            )
        )
        return SequencerResult(
            sequence_number=sequence_number,
            cancelled_side=side,
            cancelled_remaining=remaining,
            changed_bids=changes if side == SIDE_BUY else [],
            changed_asks=changes if side == SIDE_SELL else [],
        )

    def _execute_amend(self, command: AmendOrderCommand) -> SequencerResult:
        sequence_number = command.sequence_number or self.next_sequence(self.symbol)
        if command.sequence_number is not None and self.observe_sequence is not None:
            self.observe_sequence(self.symbol, sequence_number)
        amend = self.engine.amend_order(
            self.symbol,
            command.order_id,
            command.new_price,
            command.new_remaining,
            sequence_number=sequence_number,
        )
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="order_amended",
                symbol=self.symbol,
                sequence_number=sequence_number,
                order_id=command.order_id,
                payload={
                    "new_price": decimal_payload(command.new_price),
                    "new_remaining": decimal_payload(command.new_remaining),
                    "kept_priority": None if amend is None else amend.kept_priority,
                    "priority_sequence_number": None if amend is None else amend.sequence_number,
                },
            )
        )
        return SequencerResult(
            sequence_number=sequence_number,
            amend_result=amend,
            changed_bids=[] if amend is None else amend.changed_bids,
            changed_asks=[] if amend is None else amend.changed_asks,
        )

    def _execute_crossing_amend(self, command: CrossingAmendCommand) -> SequencerResult:
        sequence_number = command.sequence_number or self.next_sequence(self.symbol)
        if command.sequence_number is not None and self.observe_sequence is not None:
            self.observe_sequence(self.symbol, sequence_number)
        side, _, old_changes = self.engine.cancel_order(self.symbol, command.order_id)
        if side is None:
            self.outbox.publish_nowait(
                ExecutionEvent(
                    event_type="order_amended",
                    symbol=self.symbol,
                    sequence_number=sequence_number,
                    order_id=command.order_id,
                    payload={"error": "order_not_found_on_book"},
                )
            )
            return SequencerResult(sequence_number=sequence_number, cancelled_side=None)
        result = self.engine.process_order(
            symbol=self.symbol,
            order_id=command.order_id,
            user_id=command.user_id,
            side=command.side,
            quantity=command.new_remaining,
            created_at=command.created_at,
            limit_price=command.new_price,
            can_rest=True,
            sequence_number=sequence_number,
            stp_account_key=command.stp_account_key,
            stp_group_key=command.stp_group_key,
            stp_is_bot=command.stp_is_bot,
            stp_mode=command.stp_mode,
            stp_policy=command.stp_policy,
        )
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="order_amended",
                symbol=self.symbol,
                sequence_number=sequence_number,
                order_id=command.order_id,
                payload={
                    "new_price": decimal_payload(command.new_price),
                    "new_remaining": decimal_payload(command.new_remaining),
                    "crossing_book": True,
                    "priority_sequence_number": sequence_number,
                },
            )
        )
        for fill in result.fills:
            self.outbox.publish_nowait(
                ExecutionEvent(
                    event_type="order_matched",
                    symbol=self.symbol,
                    sequence_number=sequence_number,
                    order_id=command.order_id,
                    payload={
                        "maker_order_id": fill.maker_order_id,
                        "maker_user_id": fill.maker_user_id,
                        "price": decimal_payload(fill.price),
                        "quantity": decimal_payload(fill.quantity),
                    },
                )
            )
        changed_bids = list(result.changed_bids)
        changed_asks = list(result.changed_asks)
        if side == SIDE_BUY:
            changed_bids = old_changes + changed_bids
        else:
            changed_asks = old_changes + changed_asks
        return SequencerResult(
            sequence_number=sequence_number,
            engine_result=result,
            cancelled_side=side,
            changed_bids=changed_bids,
            changed_asks=changed_asks,
        )

    def _execute_batch_amend(self, command: BatchAmendCommand) -> SequencerResult:
        batch_sequence = self.next_sequence(self.symbol)
        requests: list[AmendOrderRequest] = []
        item_sequences: dict[str, int] = {}
        for item in command.items:
            item_sequence = self.next_sequence(self.symbol)
            item_sequences[item.order_id] = item_sequence
            requests.append(
                AmendOrderRequest(
                    order_id=item.order_id,
                    new_price=item.new_price,
                    new_remaining=item.new_remaining,
                    sequence_number=item_sequence,
                )
            )
        # This entire loop is pure memory work. No DB, K-line or WebSocket I/O is
        # allowed here, so a batch amend cannot be interleaved by persistence.
        batch = self.engine.batch_amend_orders(self.symbol, requests)
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="batch_amended",
                symbol=self.symbol,
                sequence_number=batch_sequence,
                payload={
                    "count": len(command.items),
                    "order_sequences": item_sequences,
                    "failed_order_ids": [order_id for order_id, result in batch.results.items() if result is None],
                },
            )
        )
        return SequencerResult(
            sequence_number=batch_sequence,
            batch_amend_result=batch,
            changed_bids=batch.changed_bids,
            changed_asks=batch.changed_asks,
        )

    def _execute_bulk_quote_patch(self, command: BulkQuotePatchCommand) -> SequencerResult:
        batch_sequence = self.next_sequence(self.symbol)
        # The book normalizes cancel/shrink/amend/place phases. Allocate
        # priority at actual execution, never before that reorder.
        for operation in command.operations:
            if operation.sequence_number and self.observe_sequence is not None:
                self.observe_sequence(self.symbol, operation.sequence_number)
        result = self.engine.bulk_quote_patch(
            self.symbol,
            command.operations,
            sequence_number=batch_sequence,
            next_priority=lambda: self.next_sequence(self.symbol),
        )
        self.outbox.publish_nowait(
            ExecutionEvent(
                event_type="bulk_quote_patch",
                symbol=self.symbol,
                sequence_number=batch_sequence,
                payload={
                    "accepted_count": result.accepted_count,
                    "changed_count": result.changed_count,
                    "operation_counts": dict(result.operation_counts),
                },
            )
        )
        for fill in result.fills:
            self.outbox.publish_nowait(
                ExecutionEvent(
                    event_type="order_matched",
                    symbol=self.symbol,
                    sequence_number=batch_sequence,
                    order_id=None,
                    payload={
                        "maker_order_id": fill.maker_order_id,
                        "maker_user_id": fill.maker_user_id,
                        "price": decimal_payload(fill.price),
                        "quantity": decimal_payload(fill.quantity),
                    },
                )
            )
        return SequencerResult(
            sequence_number=batch_sequence,
            bulk_quote_patch_result=result,
            changed_bids=result.changed_bids,
            changed_asks=result.changed_asks,
        )
