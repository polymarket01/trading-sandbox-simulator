from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP


def to_decimal(value: str | int | float | Decimal | None) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_step(value: Decimal, step: Decimal) -> Decimal:
    if step == 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def quantize_scale(value: Decimal | str | int | float, scale: int) -> Decimal:
    decimal_value = to_decimal(value)
    quantum = Decimal("1").scaleb(-scale)
    return decimal_value.quantize(quantum, rounding=ROUND_HALF_UP)


def decimal_scale(value: Decimal | str | int | float) -> int:
    exponent = to_decimal(value).normalize().as_tuple().exponent
    return max(0, -exponent)


def is_step_aligned(value: Decimal, step: Decimal) -> bool:
    if step == 0:
        return True
    return quantize_step(value, step) == value


def decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
