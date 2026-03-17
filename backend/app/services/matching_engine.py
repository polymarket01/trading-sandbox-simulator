from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sortedcontainers import SortedDict

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.core.decimal_utils import decimal_to_str


@dataclass(slots=True)
class BookOrder:
    order_id: str
    user_id: int
    side: str
    price: Decimal
    remaining: Decimal
    created_at: datetime


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


@dataclass(slots=True)
class AmendResult:
    kept_priority: bool
    changed_bids: list[list[str]] = field(default_factory=list)
    changed_asks: list[list[str]] = field(default_factory=list)


@dataclass(slots=True)
class BookNode:
    order_id: str
    user_id: int
    side: str
    price: Decimal
    remaining: Decimal
    created_at: datetime
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
    def __init__(self) -> None:
        self.bids = SortedDict()
        self.asks = SortedDict()
        self.orders: dict[str, BookNode] = {}

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

    def add_resting_order(self, order: BookOrder) -> list[list[str]]:
        node = BookNode(
            order_id=order.order_id,
            user_id=order.user_id,
            side=order.side,
            price=order.price,
            remaining=order.remaining,
            created_at=order.created_at,
        )
        return self._attach_node(node)

    def cancel_order(self, order_id: str) -> tuple[str | None, Decimal | None, list[list[str]]]:
        node = self.orders.get(order_id)
        if node is None:
            return None, None, []
        side = node.side
        remaining = node.remaining
        changes = self._detach_node(node, drop_from_index=True)
        return side, remaining, changes

    def amend_order(self, order_id: str, new_price: Decimal, new_remaining: Decimal) -> AmendResult | None:
        node = self.orders.get(order_id)
        if node is None:
            return None
        side = node.side
        old_price = node.price
        old_remaining = node.remaining

        if new_price == old_price and new_remaining == old_remaining:
            return AmendResult(kept_priority=True)

        if new_price == old_price and new_remaining < old_remaining:
            level = node.level
            if level is None:
                return None
            level.total_remaining -= old_remaining - new_remaining
            node.remaining = new_remaining
            changes = self._level_change(old_price, level.total_remaining)
            return AmendResult(
                kept_priority=True,
                changed_bids=changes if side == SIDE_BUY else [],
                changed_asks=changes if side == SIDE_SELL else [],
            )

        if new_price == old_price:
            self._detach_node(node, drop_from_index=False)
            node.remaining = new_remaining
            changes = self._attach_node(node)
            return AmendResult(
                kept_priority=False,
                changed_bids=changes if side == SIDE_BUY else [],
                changed_asks=changes if side == SIDE_SELL else [],
            )

        old_changes = self._detach_node(node, drop_from_index=False)
        node.price = new_price
        node.remaining = new_remaining
        new_changes = self._attach_node(node)
        return AmendResult(
            kept_priority=False,
            changed_bids=(old_changes + new_changes) if side == SIDE_BUY else [],
            changed_asks=(old_changes + new_changes) if side == SIDE_SELL else [],
        )

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
    ) -> EngineResult:
        result = EngineResult(remaining_quantity=quantity)
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
            take_qty = min(result.remaining_quantity, maker_node.remaining)
            maker_node.remaining -= take_qty
            level.total_remaining -= take_qty
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

        if can_rest and result.remaining_quantity > 0 and limit_price is not None:
            changes = self.add_resting_order(
                BookOrder(
                    order_id=order_id,
                    user_id=user_id,
                    side=side,
                    price=limit_price,
                    remaining=result.remaining_quantity,
                    created_at=created_at,
                )
            )
            if side == SIDE_BUY:
                result.changed_bids.extend(changes)
            else:
                result.changed_asks.extend(changes)
            result.placed_on_book = True
        return result


class MatchingEngine:
    def __init__(self) -> None:
        self.books: dict[str, MarketBook] = {}

    def ensure_market(self, symbol: str) -> MarketBook:
        book = self.books.get(symbol)
        if book is None:
            book = MarketBook()
            self.books[symbol] = book
        return book

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
    ) -> EngineResult:
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
        )

    def cancel_order(self, symbol: str, order_id: str) -> tuple[str | None, Decimal | None, list[list[str]]]:
        return self.ensure_market(symbol).cancel_order(order_id)

    def amend_order(self, symbol: str, order_id: str, new_price: Decimal, new_remaining: Decimal) -> AmendResult | None:
        return self.ensure_market(symbol).amend_order(order_id, new_price, new_remaining)

    def load_resting_order(self, symbol: str, order: BookOrder) -> None:
        self.ensure_market(symbol).add_resting_order(order)
