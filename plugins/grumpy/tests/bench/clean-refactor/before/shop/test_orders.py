from decimal import Decimal

from .invoice import render
from .orders import calc


def test_calc_sums_price_times_quantity():
    assert calc([("1.50", 2), ("0.25", 4)]) == Decimal("4.00")


def test_calc_empty_order_is_zero():
    assert calc([]) == Decimal("0")


def test_render_ends_with_total():
    assert render([("2.00", 1)]).splitlines()[-1] == "Total: 2.00"
