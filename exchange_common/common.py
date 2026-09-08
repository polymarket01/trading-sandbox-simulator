from __future__ import annotations

import hashlib
import hmac
import os
import sys
import time
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any


BACKEND_VENV_PYTHON = Path(__file__).resolve().parents[1] / "backend" / ".venv" / "bin" / "python"
ENTRYPOINT = Path(__file__).resolve().parents[1] / "mm_simple_test.py"


def ensure_runtime_modules():
    missing: list[str] = []
    try:
        import httpx as _httpx
    except ModuleNotFoundError:
        missing.append("httpx")
        _httpx = None
    try:
        import websockets as _websockets
    except ModuleNotFoundError:
        missing.append("websockets")
        _websockets = None

    if missing:
        current = Path(sys.executable).resolve()
        if BACKEND_VENV_PYTHON.exists() and current != BACKEND_VENV_PYTHON.resolve():
            os.execv(str(BACKEND_VENV_PYTHON), [str(BACKEND_VENV_PYTHON), str(ENTRYPOINT), *sys.argv[1:]])
        raise RuntimeError(
            "当前 Python 环境缺少依赖: "
            + ", ".join(missing)
            + "。建议直接运行 backend/.venv/bin/python mm_simple_test.py"
        )
    return _httpx, _websockets


httpx, websockets = ensure_runtime_modules()

ZERO = Decimal("0")
ONE = Decimal("1")
BPS_BASE = Decimal("10000")
LIVE_ORDER_STATUSES = {"new", "partially_filled"}
TERMINAL_ORDER_STATUSES = {"filled", "canceled", "rejected"}


def now_ms() -> int:
    return int(time.time() * 1000)


def decimal_from(value: Any) -> Decimal:
    return Decimal(str(value))


def clamp_decimal(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
    return max(lower, min(upper, value))


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_DOWN)
    return (units * step).quantize(step.normalize())


def ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    units = (value / step).to_integral_value(rounding=ROUND_CEILING)
    return (units * step).quantize(step.normalize())


def bps_between(newer: Decimal, older: Decimal) -> Decimal:
    if older <= ZERO:
        return ZERO
    return (newer - older) / older * BPS_BASE


def abs_bps_between(left: Decimal, right: Decimal) -> Decimal:
    return abs(bps_between(left, right))


def random_decimal(lower: Decimal, upper: Decimal) -> Decimal:
    import random

    if lower >= upper:
        return lower
    return lower + (upper - lower) * Decimal(str(random.random()))


def sign_ws_signature(api_key: str, api_secret: str, timestamp: int) -> str:
    payload = f"{api_key}:{timestamp}".encode()
    return hmac.new(api_secret.encode(), payload, hashlib.sha256).hexdigest()


def ratio_to_bps(ratio: Decimal) -> Decimal:
    return ratio * BPS_BASE


def price_for_side(side: str, raw_price: Decimal, tick: Decimal) -> Decimal:
    if side == "buy":
        return floor_to_step(raw_price, tick)
    return ceil_to_step(raw_price, tick)


def qty_to_step(raw_qty: Decimal, step: Decimal) -> Decimal:
    return floor_to_step(raw_qty, step)


def client_order_id(kind: str, account_name: str, tag: str) -> str:
    return f"{kind}-{account_name}-{tag}-{now_ms()}"


def parse_client_order_id(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    parts = value.split("-")
    if len(parts) < 4:
        return None
    kind = parts[0]
    if kind not in {"mmv2", "flowv2"}:
        return None
    account_name = parts[1]
    tag = "-".join(parts[2:-1])
    return kind, account_name, tag
