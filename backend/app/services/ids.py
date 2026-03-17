from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

_order_counter = count(1000)
_trade_counter = count(10000)
_ledger_counter = count(50000)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


def next_order_id() -> str:
    return f"o_{_stamp()}_{next(_order_counter)}"


def next_trade_id() -> str:
    return f"t_{_stamp()}_{next(_trade_counter)}"


def next_ledger_id() -> str:
    return f"l_{_stamp()}_{next(_ledger_counter)}"
