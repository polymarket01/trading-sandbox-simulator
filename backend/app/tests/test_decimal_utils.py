from decimal import Decimal

from app.core.decimal_utils import is_step_aligned, quantize_scale


def test_quantize_scale_removes_sqlite_numeric_noise() -> None:
    assert quantize_scale(Decimal("0.100000000000000006"), 1) == Decimal("0.1")
    assert quantize_scale(Decimal("0.000100000000000000"), 4) == Decimal("0.0001")


def test_step_alignment_works_after_scale_normalization() -> None:
    tick = quantize_scale(Decimal("0.100000000000000006"), 1)
    assert is_step_aligned(Decimal("74739.9"), tick)
    assert not is_step_aligned(Decimal("74739.92"), tick)
