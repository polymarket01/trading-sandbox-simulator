from __future__ import annotations

from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from typing import Protocol

from app.core.constants import SIDE_BUY, SIDE_SELL
from app.core.decimal_utils import quantize_scale
from app.models.market import Market


class SeedBookPayload(Protocol):
    mid_price: Decimal
    levels: int
    gap_ticks: int
    quantity: Decimal | None


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= Decimal("0"):
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= Decimal("0"):
        return value
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def build_seed_book_plan(market: Market, payload: SeedBookPayload) -> list[dict]:
    price_tick = Decimal(market.price_tick)
    qty_step = Decimal(market.qty_step)
    min_qty = Decimal(market.min_qty)
    min_notional = Decimal(market.min_notional)
    gap = price_tick * Decimal(payload.gap_ticks)
    base_qty = payload.quantity
    if base_qty is None:
        base_qty = max(min_qty, (min_notional / Decimal(payload.mid_price)) * Decimal("5"))
    base_qty = ceil_to_step(base_qty, qty_step)
    plan: list[dict] = []
    for level in range(1, payload.levels + 1):
        level_qty = base_qty + (qty_step * Decimal(level - 1))
        bid_price = floor_to_step(Decimal(payload.mid_price) - gap * Decimal(level), price_tick)
        ask_price = ceil_to_step(Decimal(payload.mid_price) + gap * Decimal(level), price_tick)
        for side, price in ((SIDE_BUY, bid_price), (SIDE_SELL, ask_price)):
            if price <= Decimal("0"):
                continue
            min_qty_for_notional = ceil_to_step(min_notional / price, qty_step)
            quantity = ceil_to_step(max(level_qty, min_qty, min_qty_for_notional), qty_step)
            plan.append(
                {
                    "side": side,
                    "price": quantize_scale(price, market.price_precision),
                    "quantity": quantize_scale(quantity, market.qty_precision),
                }
            )
    return plan
