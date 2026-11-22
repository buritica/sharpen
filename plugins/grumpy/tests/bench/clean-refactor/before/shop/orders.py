"""Order arithmetic shared by checkout and invoicing."""

from decimal import Decimal


def calc(items):
    """Return the order total for a list of (unit_price, quantity) pairs."""
    total = Decimal("0")
    for unit_price, quantity in items:
        total += Decimal(unit_price) * quantity
    return total
