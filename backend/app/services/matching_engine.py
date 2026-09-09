from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import logging
from time import monotonic
from weakref import WeakSet
from typing import Callable

from app.services.matching_faults import MatchingFault, BusinessRejected, BookInvariantError

from sortedcontainers import SortedDict

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.core.decimal_utils import decimal_to_str
from exchange_common.quote_pipeline import SelfTradePolicy

_engine_logger = logging.getLogger("matching_engine")
_amend_miss_last_log: dict[str, float] = {}


@dataclass(slots=True)
class BookOrder:
    order_id: str
    user_id: int
    side: str
    price: Decimal
    remaining: Decimal
    created_at: datetime
    sequence_number: int = 0
    stp_account_key: str | None = None
    stp_group_key: str | None = None
    stp_is_bot: bool = False
    stp_mode: str = "cancel_taker"


@dataclass(slots=True)
class MatchFill:
    maker_order_id: str
    maker_user_id: int
    price: Decimal
    quantity: Decimal


@dataclass(slots=True)
class EngineResult:
    fills: list[MatchFill] = field(default_factory=list)
    remaining_quantity: Decimal = Decimal("0")
    placed_on_book: bool = False
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)
    stop_reason: str | None = None
    stp_action: str | None = None
    stp_reason: str | None = None
    stp_intercept_count: int = 0
    stp_decremented_quantity: Decimal = Decimal("0")


@dataclass(slots=True)
class AmendResult:
    kept_priority: bool
    sequence_number: int | None = None
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class AmendOrderRequest:
    order_id: str
    new_price: Decimal
    new_remaining: Decimal
    sequence_number: int | None = None


@dataclass(slots=True)
class BatchAmendResult:
    results: dict[str, AmendResult | None] = field(default_factory=dict)
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class BulkQuotePatchOperation:
    """One in-memory quote patch operation.

    The public/API layer may still provide Decimal values.  The matching
    loop receives this compact slots object and never carries a Pydantic model
    or a large per-level response dictionary.
    """

    action: str
    order_id: str
    user_id: int = 0
    side: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    created_at: datetime | None = None
    sequence_number: int = 0
    can_rest: bool = True
    maker_guard: Callable[[str, int], bool] | None = None
    stp_account_key: str | None = None
    stp_group_key: str | None = None
    stp_is_bot: bool = False
    stp_mode: str = "cancel_taker"
    stp_policy: SelfTradePolicy | None = None


@dataclass(slots=True)
class BulkQuotePatchResult:
    accepted_count: int = 0
    changed_count: int = 0
    rejected_count: int = 0
    operation_counts: dict[str, int] = field(default_factory=dict)
    fills: list[MatchFill] = field(default_factory=list)
    fill_takers: list[tuple[str, int, str]] = field(default_factory=list)
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)
    sequence_number: int = 0
    stp_intercept_count: int = 0
    stp_decremented_quantity: Decimal = Decimal("0")


class BulkQuotePatchError(BusinessRejected, ValueError):
    """Raised after a failed patch has been rolled back completely."""

    def __init__(self, message: str, *, operation_index: int) -> None:
        super().__init__(message)
        self.operation_index = operation_index


@dataclass(slots=True)
class BookNode:
    order_id: str
    user_id: int
    side: str
    price: Decimal
    remaining: Decimal
    created_at: datetime
    sequence_number: int = 0
    stp_account_key: str | None = None
    stp_group_key: str | None = None
    stp_is_bot: bool = False
    stp_mode: str = "cancel_taker"
    prev: BookNode | None = None
    next: BookNode | None = None
    level: PriceLevel | None = None


class PriceLevel:
    __slots__ = ("price", "head", "tail", "total_remaining")

    def __init__(self, price: Decimal) -> None:
        self.price = price
        self.head: BookNode | None = None
        self.tail: BookNode | None = None
        self.total_remaining = Decimal("0")

    def append(self, node: BookNode) -> None:
        node.prev = self.tail
        node.next = None
        node.level = self
        if self.tail is not None:
            self.tail.next = node
        else:
            self.head = node
        self.tail = node
        self.total_remaining += node.remaining

    def remove(self, node: BookNode, *, adjust_total: bool = True) -> None:
        if node.level is not self:
            return
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self.head = node.next
        if node.next is not None:
            node.next.prev = node.prev
        else:
            self.tail = node.prev
        if adjust_total:
            self.total_remaining -= node.remaining
        node.prev = None
        node.next = None
        node.level = None

    def is_empty(self) -> bool:
        return self.head is None


class MarketBook:
    def __init__(self, fault: MatchingFault | None = None) -> None:
        self.fault = fault or MatchingFault()
        self.bids = SortedDict()
        self.asks = SortedDict()
        self.orders: dict[str, BookNode] = {}
        self.mutation_version = 0

    def _touch(self) -> None:
        self.mutation_version += 1

    def _levels(self, side: str) -> SortedDict:
        return self.bids if side == SIDE_BUY else self.asks

    def _iter_levels(self, side: str):
        levels = self._levels(side)
        keys = levels.keys()
        if side == SIDE_BUY:
            for index in range(len(keys) - 1, -1, -1):
                price = keys[index]
                yield price, levels[price]
            return
        for index in range(len(keys)):
            price = keys[index]
            yield price, levels[price]

    def _best_level(self, side: str) -> tuple[Decimal, PriceLevel] | None:
        levels = self._levels(side)
        if not levels:
            return None
        return levels.peekitem(-1 if side == SIDE_BUY else 0)

    @staticmethod
    def _level_change(price: Decimal, quantity: Decimal) -> list[list[str]]:
        return [[decimal_to_str(price), decimal_to_str(quantity)]]

    def _attach_node(self, node: BookNode) -> list[list[str]]:
        levels = self._levels(node.side)
        level = levels.get(node.price)
        if level is None:
            level = PriceLevel(node.price)
            levels[node.price] = level
        level.append(node)
        self.orders[node.order_id] = node
        self._touch()
        return self._level_change(node.price, level.total_remaining)

    def _detach_node(self, node: BookNode, *, drop_from_index: bool = True) -> list[list[str]]:
        level = node.level
        if level is None:
            return []
        side = node.side
        price = node.price
        levels = self._levels(side)
        level.remove(node, adjust_total=True)
        if level.is_empty():
            levels.pop(price, None)
            quantity = Decimal("0")
        else:
            quantity = level.total_remaining
        if drop_from_index:
            self.orders.pop(node.order_id, None)
        self._touch()
        return self._level_change(price, quantity)

    def best_bid(self) -> Decimal | None:
        best = self._best_level(SIDE_BUY)
        return best[0] if best is not None else None

    def best_ask(self) -> Decimal | None:
        best = self._best_level(SIDE_SELL)
        return best[0] if best is not None else None

    def level_quantity(self, side: str, price: Decimal) -> Decimal:
        level = self._levels(side).get(price)
        return level.total_remaining if level is not None else Decimal("0")

    def snapshot(self, depth: int | None = None) -> dict[str, list[list[str]]]:
        bids: list[list[str]] = []
        asks: list[list[str]] = []
        for count, (price, level) in enumerate(self._iter_levels(SIDE_BUY), start=1):
            bids.append([decimal_to_str(price), decimal_to_str(level.total_remaining)])
            if depth is not None and count >= depth:
                break
        for count, (price, level) in enumerate(self._iter_levels(SIDE_SELL), start=1):
            asks.append([decimal_to_str(price), decimal_to_str(level.total_remaining)])
            if depth is not None and count >= depth:
                break
        return {"bids": bids, "asks": asks}

    def reference_price(self) -> Decimal | None:
        best_bid = self.best_bid()
        best_ask = self.best_ask()
        if best_bid is not None and best_ask is not None:
            return (best_bid + best_ask) / Decimal("2")
        return best_bid or best_ask

    def simulate_cost(
        self,
        side: str,
        quantity: Decimal,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        total_qty = Decimal("0")
        total_notional = Decimal("0")
        book_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        for price, level in self._iter_levels(book_side):
            if side == SIDE_BUY and max_price is not None and price > max_price:
                break
            if side == SIDE_SELL and min_price is not None and price < min_price:
                break
            current = level.head
            while current is not None:
                take_qty = min(current.remaining, quantity - total_qty)
                total_qty += take_qty
                total_notional += take_qty * price
                if total_qty >= quantity:
                    return total_qty, total_notional
                current = current.next
        return total_qty, total_notional

    def preview_bbo_order(
        self,
        *,
        side: str,
        quantity: Decimal,
        limit_price: Decimal,
        maker_guard: Callable[[str, int, Decimal, Decimal], bool] | None = None,
        taker_account_key: str | None = None,
        taker_is_bot: bool = False,
        stp_policy: SelfTradePolicy | None = None,
    ) -> EngineResult:
        """Preview price-priority/FIFO fills across every crossed limit level."""
        result = EngineResult(remaining_quantity=quantity)
        policy = stp_policy or SelfTradePolicy()
        book_side = SIDE_SELL if side == SIDE_BUY else SIDE_BUY
        for price, level in self._iter_levels(book_side):
            if (side == SIDE_BUY and price > limit_price) or (
                side == SIDE_SELL and price < limit_price
            ):
                break
            maker_node = level.head
            while maker_node is not None and result.remaining_quantity > 0:
                decision = policy.decide(
                    taker_account_key=taker_account_key,
                    maker_account_key=maker_node.stp_account_key,
                    taker_is_bot=taker_is_bot,
                    maker_is_bot=maker_node.stp_is_bot,
                )
                if decision is not None:
                    result.stp_intercept_count += 1
                    result.stp_action = decision.mode
                    result.stp_reason = decision.reason
                    result.stop_reason = f"stp_{decision.mode}"
                    if decision.mode in {"cancel_taker", "reject"}:
                        return result
                    if decision.mode == "decrement_and_cancel":
                        decrement = min(result.remaining_quantity, maker_node.remaining)
                        result.remaining_quantity -= decrement
                        result.stp_decremented_quantity += decrement
                    maker_node = maker_node.next
                    continue
                if maker_guard is not None and not maker_guard(
                    maker_node.order_id,
                    maker_node.user_id,
                    maker_node.price,
                    maker_node.remaining,
                ):
                    result.stop_reason = "maker_not_authorized_for_synthetic_flow"
                    return result
                take_qty = min(result.remaining_quantity, maker_node.remaining)
                result.fills.append(
                    MatchFill(
                        maker_order_id=maker_node.order_id,
                        maker_user_id=maker_node.user_id,
                        price=price,
                        quantity=take_qty,
                    )
                )
                result.remaining_quantity -= take_qty
                maker_node = maker_node.next
            if result.remaining_quantity <= 0:
                break
        return result

    def add_resting_order(self, order: BookOrder) -> list[list[str]]:
        if self.fault.halted:
            self.fault.check()
        node = BookNode(
            order_id=order.order_id,
            user_id=order.user_id,
            side=order.side,
            price=order.price,
            remaining=order.remaining,
            created_at=order.created_at,
            sequence_number=order.sequence_number,
            stp_account_key=order.stp_account_key,
            stp_group_key=order.stp_group_key,
            stp_is_bot=order.stp_is_bot,
            stp_mode=order.stp_mode,
        )
        return self._attach_node(node)

    def cancel_order(self, order_id: str) -> tuple[str | None, Decimal | None, list[list[str]]]:
        if self.fault.halted:
            self.fault.check()
        node = self.orders.get(order_id)
        if node is None:
            return None, None, []
        side = node.side
        remaining = node.remaining
        changes = self._detach_node(node, drop_from_index=True)
        return side, remaining, changes

    def reconcile_open_orders(self, valid_order_ids: set[str]) -> tuple[list[list[str]], list[list[str]], list[str]]:
        if self.fault.halted:
            self.fault.check()
        changed_bids: list[list[str]] = []
        changed_asks: list[list[str]] = []
        removed: list[str] = []
        for order_id in list(self.orders):
            if order_id in valid_order_ids:
                continue
            node = self.orders.get(order_id)
            if node is None:
                continue
            side = node.side
            changes = self._detach_node(node, drop_from_index=True)
            if side == SIDE_BUY:
                changed_bids.extend(changes)
            else:
                changed_asks.extend(changes)
            removed.append(order_id)
        return changed_bids, changed_asks, removed

    def amend_order(
        self,
        order_id: str,
        new_price: Decimal,
        new_remaining: Decimal,
        *,
        sequence_number: int | None = None,
    ) -> AmendResult | None:
        if self.fault.halted:
            self.fault.check()
        node = self.orders.get(order_id)
        if node is None:
            return None
        side = node.side
        old_price = node.price
        old_remaining = node.remaining
        old_sequence_number = node.sequence_number

        if new_price == old_price and new_remaining == old_remaining:
            return AmendResult(kept_priority=True, sequence_number=old_sequence_number)

        if new_price == old_price and new_remaining < old_remaining:
            level = node.level
            if level is None:
                return None
            level.total_remaining -= old_remaining - new_remaining
            node.remaining = new_remaining
            self._touch()
            changes = self._level_change(old_price, level.total_remaining)
            return AmendResult(
                kept_priority=True,
                sequence_number=old_sequence_number,
                changed_bids=changes if side == SIDE_BUY else [],
                changed_asks=changes if side == SIDE_SELL else [],
            )

        if new_price == old_price:
            self._detach_node(node, drop_from_index=False)
            node.remaining = new_remaining
            if sequence_number is not None:
                node.sequence_number = sequence_number
            changes = self._attach_node(node)
            return AmendResult(
                kept_priority=False,
                sequence_number=node.sequence_number,
                changed_bids=changes if side == SIDE_BUY else [],
                changed_asks=changes if side == SIDE_SELL else [],
            )

        old_changes = self._detach_node(node, drop_from_index=False)
        node.price = new_price
        node.remaining = new_remaining
        if sequence_number is not None:
            node.sequence_number = sequence_number
        new_changes = self._attach_node(node)
        return AmendResult(
            kept_priority=False,
            sequence_number=node.sequence_number,
            changed_bids=(old_changes + new_changes) if side == SIDE_BUY else [],
            changed_asks=(old_changes + new_changes) if side == SIDE_SELL else [],
        )

    def batch_amend_orders(self, requests: list[AmendOrderRequest]) -> BatchAmendResult:
        if self.fault.halted:
            self.fault.check()
        batch = BatchAmendResult()
        missing_order_ids = [request.order_id for request in requests if request.order_id not in self.orders]
        if missing_order_ids:
            for request in requests:
                batch.results[request.order_id] = None
            return batch
        for request in requests:
            amend = self.amend_order(
                request.order_id,
                request.new_price,
                request.new_remaining,
                sequence_number=request.sequence_number,
            )
            batch.results[request.order_id] = amend
            if amend is None:
                continue
            batch.changed_bids.extend(amend.changed_bids)
            batch.changed_asks.extend(amend.changed_asks)
        return batch

    def process_order(
        self,
        order_id: str,
        user_id: int,
        side: str,
        quantity: Decimal,
        created_at: datetime,
        limit_price: Decimal | None = None,
        can_rest: bool = False,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
        sequence_number: int = 0,
        maker_guard: Callable[[str, int], bool] | None = None,
        stp_account_key: str | None = None,
        stp_group_key: str | None = None,
        stp_is_bot: bool = False,
        stp_mode: str = "cancel_taker",
        stp_policy: SelfTradePolicy | None = None,
    ) -> EngineResult:
        if self.fault.halted:
            self.fault.check()
        result = EngineResult(remaining_quantity=quantity)
        policy = stp_policy or SelfTradePolicy(same_account_mode=stp_mode)
        stp_blocked = False
        while result.remaining_quantity > 0:
            best = self._best_level(SIDE_SELL if side == SIDE_BUY else SIDE_BUY)
            if best is None:
                break
            best_price, level = best
            if limit_price is not None:
                if side == SIDE_BUY and best_price > limit_price:
                    break
                if side == SIDE_SELL and best_price < limit_price:
                    break
            if side == SIDE_BUY and max_price is not None and best_price > max_price:
                result.stop_reason = "protection_price_reached"
                break
            if side == SIDE_SELL and min_price is not None and best_price < min_price:
                result.stop_reason = "protection_price_reached"
                break

            maker_node = level.head
            if maker_node is None:
                break
            decision = policy.decide(
                taker_account_key=stp_account_key,
                maker_account_key=maker_node.stp_account_key,
                taker_is_bot=stp_is_bot,
                maker_is_bot=maker_node.stp_is_bot,
            )
            if decision is not None:
                result.stp_intercept_count += 1
                result.stp_action = decision.mode
                result.stp_reason = decision.reason
                result.stop_reason = f"stp_{decision.mode}"
                if decision.mode in {"cancel_taker", "reject"}:
                    stp_blocked = True
                    break
                if decision.mode == "cancel_maker":
                    old_changes = self._detach_node(maker_node, drop_from_index=True)
                    target = result.changed_bids if maker_node.side == SIDE_BUY else result.changed_asks
                    target.extend(old_changes)
                    continue
                if decision.mode == "decrement_and_cancel":
                    maker_remaining = maker_node.remaining
                    decrement = min(result.remaining_quantity, maker_remaining)
                    old_changes = self._detach_node(maker_node, drop_from_index=True)
                    result.remaining_quantity -= decrement
                    result.stp_decremented_quantity += decrement
                    target = result.changed_bids if maker_node.side == SIDE_BUY else result.changed_asks
                    target.extend(old_changes)
                    continue
            if maker_guard is not None and not maker_guard(maker_node.order_id, maker_node.user_id):
                # Hyperliquid-style: a resting order that no longer passes the
                # margin/solvency check must not keep being matched. Remove it
                # from the book and continue with the next order at this level.
                old_changes = self._detach_node(maker_node, drop_from_index=True)
                _engine_logger.warning(
                    "engine maker_guard removed resting order order_id=%s user_id=%s side=%s price=%s",
                    maker_node.order_id,
                    maker_node.user_id,
                    maker_node.side,
                    maker_node.price,
                )
                if maker_node.side == SIDE_BUY:
                    result.changed_bids.extend(old_changes)
                else:
                    result.changed_asks.extend(old_changes)
                continue
            take_qty = min(result.remaining_quantity, maker_node.remaining)
            maker_node.remaining -= take_qty
            level.total_remaining -= take_qty
            self._touch()
            result.remaining_quantity -= take_qty
            result.fills.append(
                MatchFill(
                    maker_order_id=maker_node.order_id,
                    maker_user_id=maker_node.user_id,
                    price=best_price,
                    quantity=take_qty,
                )
            )
            if maker_node.remaining <= 0:
                level.remove(maker_node, adjust_total=False)
                self.orders.pop(maker_node.order_id, None)
                if level.is_empty():
                    self._levels(maker_node.side).pop(best_price, None)
                    new_qty = Decimal("0")
                else:
                    new_qty = level.total_remaining
            else:
                new_qty = level.total_remaining
            target = result.changed_asks if maker_node.side == SIDE_SELL else result.changed_bids
            target.append([decimal_to_str(best_price), decimal_to_str(new_qty)])

        if can_rest and not stp_blocked and result.remaining_quantity > 0 and limit_price is not None:
            changes = self.add_resting_order(
                BookOrder(
                    order_id=order_id,
                    user_id=user_id,
                    side=side,
                    price=limit_price,
                    remaining=result.remaining_quantity,
                    created_at=created_at,
                    sequence_number=sequence_number,
                    stp_account_key=stp_account_key,
                    stp_group_key=stp_group_key,
                    stp_is_bot=stp_is_bot,
                    stp_mode=stp_mode,
                )
            )
            if side == SIDE_BUY:
                result.changed_bids.extend(changes)
            else:
                result.changed_asks.extend(changes)
            result.placed_on_book = True
        return result

    def iter_fifo_nodes(self):
        """Cold-path traversal in actual side/price/head-to-next order."""
        for side in (SIDE_BUY, SIDE_SELL):
            for _price, level in self._iter_levels(side):
                node = level.head
                while node is not None:
                    yield node
                    node = node.next

    def _checkpoint(self) -> tuple[list[BookOrder], int]:
        return (
            [
                BookOrder(
                    order_id=node.order_id,
                    user_id=node.user_id,
                    side=node.side,
                    price=node.price,
                    remaining=node.remaining,
                    created_at=node.created_at,
                    sequence_number=node.sequence_number,
                    stp_account_key=node.stp_account_key,
                    stp_group_key=node.stp_group_key,
                    stp_is_bot=node.stp_is_bot,
                    stp_mode=node.stp_mode,
                )
                for node in self.iter_fifo_nodes()
            ],
            self.mutation_version,
        )

    def _restore_checkpoint(self, checkpoint: tuple[list[BookOrder], int]) -> None:
        orders, version = checkpoint
        self.bids = SortedDict()
        self.asks = SortedDict()
        self.orders = {}
        for order in orders:
            node = BookNode(
                order_id=order.order_id,
                user_id=order.user_id,
                side=order.side,
                price=order.price,
                remaining=order.remaining,
                created_at=order.created_at,
                sequence_number=order.sequence_number,
                stp_account_key=order.stp_account_key,
                stp_group_key=order.stp_group_key,
                stp_is_bot=order.stp_is_bot,
                stp_mode=order.stp_mode,
            )
            self._attach_node(node)
        # A failed transaction is invisible to publishers; restore the exact
        # pre-batch version after rebuilding the private linked lists.
        self.mutation_version = version

    def validate_invariants(self) -> None:
        try:
            indexed: set[str] = set()
            for side, levels in ((SIDE_BUY, self.bids), (SIDE_SELL, self.asks)):
                for price, level in levels.items():
                    if level.price != price or level.is_empty():
                        raise BookInvariantError("empty or mismatched price level")
                    total = Decimal("0")
                    previous = None
                    node = level.head
                    while node is not None:
                        if node.side != side or node.price != price or node.level is not level or node.prev is not previous:
                            raise BookInvariantError("broken price-time links")
                        if node.order_id in indexed or node.remaining <= 0 or self.orders.get(node.order_id) is not node:
                            raise BookInvariantError("duplicate or non-positive resting order")
                        indexed.add(node.order_id)
                        total += node.remaining
                        previous = node
                        node = node.next
                    if previous is not level.tail or total != level.total_remaining:
                        raise BookInvariantError("price level quantity invariant failed")
            if indexed != set(self.orders):
                raise BookInvariantError("order index invariant failed")
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT")
            raise

    def ordered_patch_operations(self, operations):
        indexed_operations = list(enumerate(operations))

        def priority(item: tuple[int, BulkQuotePatchOperation]) -> tuple[int, int]:
            index, operation = item
            if operation.action == "cancel":
                return 0, index
            if operation.action in {"shrink", "release"}:
                return 1, index
            if operation.action in {"amend", "price_change", "quantity_change", "priority_reset"}:
                node = self.orders.get(operation.order_id)
                shrinking = node is not None and operation.quantity is not None and operation.quantity < node.remaining
                return (1 if shrinking else 2), index
            if operation.action == "place":
                return 3, index
            return 4, index

        indexed_operations.sort(key=priority)
        return indexed_operations

    def bulk_quote_patch(
        self,
        operations: list[BulkQuotePatchOperation],
        *,
        sequence_number: int = 0,
        next_priority: Callable[[], int] | None = None,
    ) -> BulkQuotePatchResult:
        """Apply one quote patch transaction inside this market's single writer.

        The caller's sequencer is the market writer/lock.  This method does no
        I/O, takes no per-operation lock, and runs exactly one invariant check
        after the ordered cancel -> release/shrink -> amend -> place phases.
        Any exception rebuilds the linked lists from a private checkpoint.
        """
        if self.fault.halted:
            self.fault.check()
        result = BulkQuotePatchResult(sequence_number=int(sequence_number))
        if not operations:
            # No mutation means no rollback image is needed. Retain the same
            # invariant responsibility even on an empty diagnostic patch.
            self.validate_invariants()
            return result
        checkpoint = self._checkpoint()
        indexed_operations = self.ordered_patch_operations(operations)
        try:
            for original_index, operation in indexed_operations:
                if next_priority is not None:
                    operation.sequence_number = next_priority()
                action = str(operation.action)
                if action == "cancel":
                    side, _remaining, changes = self.cancel_order(operation.order_id)
                    if side is None:
                        raise BulkQuotePatchError(
                            f"order not found for cancel: {operation.order_id}",
                            operation_index=original_index,
                        )
                    target = result.changed_bids if side == SIDE_BUY else result.changed_asks
                    target.extend(changes)
                elif action in {"amend", "shrink", "release", "price_change", "quantity_change", "priority_reset"}:
                    if operation.price is None or operation.quantity is None:
                        raise BulkQuotePatchError(
                            f"amend requires price and quantity: {operation.order_id}",
                            operation_index=original_index,
                        )
                    node = self.orders.get(operation.order_id)
                    if node is None:
                        raise BulkQuotePatchError(f"order not found for amend: {operation.order_id}", operation_index=original_index)
                    if not operation.price.is_finite() or not operation.quantity.is_finite() or operation.price <= 0 or operation.quantity <= 0:
                        raise BulkQuotePatchError("amend requires positive finite price and quantity", operation_index=original_index)
                    best = self.best_ask() if node.side == SIDE_BUY else self.best_bid()
                    crossing = best is not None and (operation.price >= best if node.side == SIDE_BUY else operation.price <= best)
                    if crossing:
                        side, _, changes = self.cancel_order(node.order_id)
                        target = result.changed_bids if side == SIDE_BUY else result.changed_asks
                        target.extend(changes)
                        matched = self.process_order(
                            order_id=node.order_id, user_id=node.user_id, side=node.side,
                            quantity=operation.quantity, created_at=node.created_at,
                            limit_price=operation.price, can_rest=operation.can_rest,
                            sequence_number=operation.sequence_number,
                            maker_guard=operation.maker_guard,
                            stp_account_key=node.stp_account_key, stp_group_key=node.stp_group_key,
                            stp_is_bot=node.stp_is_bot, stp_mode=node.stp_mode, stp_policy=operation.stp_policy,
                        )
                        result.fills.extend(matched.fills)
                        result.fill_takers.extend([(node.order_id, node.user_id, node.side)] * len(matched.fills))
                        result.changed_bids.extend(matched.changed_bids)
                        result.changed_asks.extend(matched.changed_asks)
                        result.stp_intercept_count += matched.stp_intercept_count
                        result.stp_decremented_quantity += matched.stp_decremented_quantity
                    else:
                        amend = self.amend_order(node.order_id, operation.price, operation.quantity,
                                                 sequence_number=operation.sequence_number or None)
                        result.changed_bids.extend(amend.changed_bids)
                        result.changed_asks.extend(amend.changed_asks)
                elif action == "place":
                    if operation.side not in {SIDE_BUY, SIDE_SELL}:
                        raise BulkQuotePatchError("place requires a valid side", operation_index=original_index)
                    if operation.price is None or operation.quantity is None or operation.created_at is None:
                        raise BulkQuotePatchError(
                            f"place requires price, quantity and created_at: {operation.order_id}",
                            operation_index=original_index,
                        )
                    if operation.order_id in self.orders:
                        raise BulkQuotePatchError(
                            f"duplicate resting order: {operation.order_id}",
                            operation_index=original_index,
                        )
                    placed = self.process_order(
                        order_id=operation.order_id,
                        user_id=operation.user_id,
                        side=operation.side,
                        quantity=operation.quantity,
                        created_at=operation.created_at,
                        limit_price=operation.price,
                        can_rest=operation.can_rest,
                        sequence_number=operation.sequence_number,
                        maker_guard=operation.maker_guard,
                        stp_account_key=operation.stp_account_key,
                        stp_group_key=operation.stp_group_key,
                        stp_is_bot=operation.stp_is_bot,
                        stp_mode=operation.stp_mode,
                        stp_policy=operation.stp_policy,
                    )
                    result.fills.extend(placed.fills)
                    result.fill_takers.extend([(operation.order_id, operation.user_id, str(operation.side))] * len(placed.fills))
                    result.changed_bids.extend(placed.changed_bids)
                    result.changed_asks.extend(placed.changed_asks)
                    result.stp_intercept_count += placed.stp_intercept_count
                    result.stp_decremented_quantity += placed.stp_decremented_quantity
                else:
                    raise BulkQuotePatchError(f"unsupported quote patch action: {action}", operation_index=original_index)
                result.accepted_count += 1
                result.changed_count += 1
                result.operation_counts[action] = result.operation_counts.get(action, 0) + 1
            self.validate_invariants()
        except Exception:
            self._restore_checkpoint(checkpoint)
            result.rejected_count = 1
            raise
        return result


class MatchingEngine:
    def __init__(self) -> None:
        self.fault = MatchingFault()
        self.books: dict[str, MarketBook] = {}
        self.sequencers = WeakSet()

    def ensure_market(self, symbol: str) -> MarketBook:
        book = self.books.get(symbol)
        if book is None:
            book = MarketBook(self.fault)
            self.books[symbol] = book
        return book

    def market_version(self, symbol: str) -> int:
        return int(self.ensure_market(symbol).mutation_version)

    def snapshot(self, symbol: str, depth: int | None = None) -> dict[str, list[list[str]]]:
        return self.ensure_market(symbol).snapshot(depth)

    def reference_price(self, symbol: str) -> Decimal | None:
        return self.ensure_market(symbol).reference_price()

    def simulate_cost(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        return self.ensure_market(symbol).simulate_cost(side, quantity, max_price=max_price, min_price=min_price)

    def preview_bbo_order(
        self,
        symbol: str,
        *,
        side: str,
        quantity: Decimal,
        limit_price: Decimal,
        maker_guard: Callable[[str, int, Decimal, Decimal], bool] | None = None,
        taker_account_key: str | None = None,
        taker_is_bot: bool = False,
        stp_policy: SelfTradePolicy | None = None,
    ) -> EngineResult:
        return self.ensure_market(symbol).preview_bbo_order(
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            maker_guard=maker_guard,
            taker_account_key=taker_account_key,
            taker_is_bot=taker_is_bot,
            stp_policy=stp_policy,
        )

    def process_order(
        self,
        symbol: str,
        order_id: str,
        user_id: int,
        side: str,
        quantity: Decimal,
        created_at: datetime,
        limit_price: Decimal | None = None,
        can_rest: bool = False,
        max_price: Decimal | None = None,
        min_price: Decimal | None = None,
        sequence_number: int = 0,
        maker_guard: Callable[[str, int], bool] | None = None,
        stp_account_key: str | None = None,
        stp_group_key: str | None = None,
        stp_is_bot: bool = False,
        stp_mode: str = "cancel_taker",
        stp_policy: SelfTradePolicy | None = None,
    ) -> EngineResult:
        try:
            return self.ensure_market(symbol).process_order(
                order_id=order_id,
                user_id=user_id,
                side=side,
                quantity=quantity,
                created_at=created_at,
                limit_price=limit_price,
                can_rest=can_rest,
                max_price=max_price,
                min_price=min_price,
                sequence_number=sequence_number,
                maker_guard=maker_guard,
                stp_account_key=stp_account_key,
                stp_group_key=stp_group_key,
                stp_is_bot=stp_is_bot,
                stp_mode=stp_mode,
                stp_policy=stp_policy,
            )
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def cancel_order(self, symbol: str, order_id: str) -> tuple[str | None, Decimal | None, list[list[str]]]:
        try:
            return self.ensure_market(symbol).cancel_order(order_id)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def amend_order(
        self,
        symbol: str,
        order_id: str,
        new_price: Decimal,
        new_remaining: Decimal,
        *,
        sequence_number: int | None = None,
    ) -> AmendResult | None:
        try:
            amend = self.ensure_market(symbol).amend_order(
                order_id,
                new_price,
                new_remaining,
                sequence_number=sequence_number,
            )
            if amend is None:
                # 镜像/引擎分叉时策略 QuoteSet 会以每秒数十次的速度重试同一批
                # amend，逐条 WARNING 会把日志刷到数百 MB。按市场限速告警，
                # 分叉本身由机器人报价幽灵驱逐自愈。
                now = monotonic()
                if now - _amend_miss_last_log.get(symbol, 0.0) >= 1.0:
                    _amend_miss_last_log[symbol] = now
                    _engine_logger.warning(
                        "engine amend_order returned None symbol=%s order_id=%s new_price=%s new_remaining=%s",
                        symbol,
                        order_id,
                        new_price,
                        new_remaining,
                    )
            return amend
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def batch_amend_orders(self, symbol: str, requests: list[AmendOrderRequest]) -> BatchAmendResult:
        try:
            return self.ensure_market(symbol).batch_amend_orders(requests)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def bulk_quote_patch(
        self,
        symbol: str,
        operations: list[BulkQuotePatchOperation],
        *,
        sequence_number: int = 0,
        next_priority: Callable[[], int] | None = None,
    ) -> BulkQuotePatchResult:
        try:
            return self.ensure_market(symbol).bulk_quote_patch(operations, sequence_number=sequence_number, next_priority=next_priority)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def load_resting_order(self, symbol: str, order: BookOrder) -> None:
        try:
            self.ensure_market(symbol).add_resting_order(order)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def reconcile_open_orders(self, symbol: str, valid_order_ids: set[str]) -> tuple[list[list[str]], list[list[str]], list[str]]:
        try:
            return self.ensure_market(symbol).reconcile_open_orders(valid_order_ids)
        except BusinessRejected:
            raise
        except Exception as exc:
            self.fault.halt(exc, category="INVARIANT" if isinstance(exc, BookInvariantError) else "UNKNOWN")
            raise

    def rebuild_market(self, symbol: str, orders: list[BookOrder]) -> dict:
        self.fault.check()
        ordered, report = recovery_priority_order(orders)
        candidate = MarketBook()
        for order in ordered:
            if order.order_id in candidate.orders:
                raise ValueError("duplicate authoritative recovery order")
            candidate.add_resting_order(order)
        candidate.validate_invariants()
        candidate.fault = self.fault
        self.books[symbol] = candidate
        if not report["fifo_exact"]:
            _engine_logger.warning("recovery FIFO compatibility symbol=%s ambiguous_levels=%s", symbol, report["ambiguous_levels"])
        return report

    def clear_market(self, symbol: str) -> None:
        self.fault.check()
        self.books.pop(symbol, None)


def recovery_priority_order(orders: list[BookOrder]) -> tuple[list[BookOrder], dict]:
    """Cold merge of authoritative records, after source-specific deduplication.

    sequence_number is the market priority clock (shrink keeps it, requeue
    replaces it). Missing/tied priorities cannot prove historical FIFO: stable
    source order is a compatibility fallback, never invented timestamps.
    """
    seen = set()
    ambiguous = set()
    for order in orders:
        level = (order.side, order.price)
        priority = order.sequence_number
        key = (*level, priority)
        if type(priority) is not int or priority <= 0 or key in seen:
            ambiguous.add(level)
        seen.add(key)
    def key(order):
        value = order.sequence_number
        return (0, value) if type(value) is int and value > 0 else (1, 0)
    ordered = sorted(orders, key=key)
    report = {"fifo_exact": not ambiguous, "ambiguous_levels": len(ambiguous),
              "fallback": None if not ambiguous else "known_priority_then_stable_source_order"}
    return ordered, report
