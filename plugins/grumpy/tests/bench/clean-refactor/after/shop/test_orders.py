from decimal import Decimal

from .invoice import render
from .orders import order_total


def test_order_total_sums_price_times_quantity():
    assert order_total([("1.50", 2), ("0.25", 4)]) == Decimal("4.00")


def test_order_total_empty_order_is_zero():
    assert order_total([]) == Decimal("0")


def test_render_ends_with_total():
    assert render([("2.00", 1)]).splitlines()[-1] == "Total: 2.00"
