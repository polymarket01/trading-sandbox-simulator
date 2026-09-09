"""Pure fill semantics shared by SQL, projections and memory settlement."""
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class FillSplit:
    close: Decimal
    opened: Decimal

    @property
    def action(self) -> str:
        return 'reverse' if self.close and self.opened else 'close' if self.close else 'open'


def split_fill(*, side: str, action: str | None, reduce_only: bool,
               hedge: bool, position_side: str, position_qty: Decimal,
               quantity: Decimal) -> FillSplit:
    target = 'long' if side == 'buy' else 'short'
    reducing = action == 'close' or reduce_only
    opposing = position_qty > ZERO and position_side not in ('flat', target)
    if reducing:
        if not opposing or quantity > position_qty:
            raise ValueError('close quantity exceeds current position')
        return FillSplit(quantity, ZERO)
    if hedge:
        if position_qty > ZERO and position_side not in ('flat', target):
            raise ValueError('hedge position direction conflicts with open order')
        return FillSplit(ZERO, quantity)
    close = min(position_qty, quantity) if opposing else ZERO
    return FillSplit(close, quantity - close)


def required_reserves(position_side, position_qty, orders, *, hedge=False, available=None):
    """Allocate each unit of close capacity once, in accepted order priority.

    Input rows are small value objects (order_id/side/reduce_only/action/leaves/
    price/leverage). Explicit closes take capacity first. Fee funding is checked
    at final fill admission, independently of initial margin.
    """
    capacity = max(position_qty, ZERO)
    result = {}
    rows = sorted(orders, key=lambda o: (not (o.position_action == 'close' or o.reduce_only), o.sequence_number, o.order_id))
    for o in rows:
        qty = max(o.remaining_quantity, ZERO)
        target = 'long' if o.side == 'buy' else 'short'
        opposing = capacity > ZERO and position_side not in ('flat', target)
        reducing = o.position_action == 'close' or o.reduce_only
        closed = min(qty, capacity) if opposing and (reducing or not hedge) else ZERO
        if reducing:
            if closed + Decimal("1e-10") < qty:
                result[o.order_id] = None
                continue
            result[o.order_id] = ZERO
        else:
            result[o.order_id] = max(qty - closed, ZERO) * o.price / o.leverage
        reserve = result[o.order_id]
        if available is not None:
            if reserve > available + Decimal("1e-10"):
                continue  # Canceling this order also returns its close capacity.
            available -= reserve
        capacity = max(capacity - closed, ZERO)
    return result
