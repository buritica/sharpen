"""Render a plain-text invoice."""

from .orders import order_total


def render(items):
    lines = [f"{qty} x {price}" for price, qty in items]
    lines.append(f"Total: {order_total(items)}")
    return "\n".join(lines)
