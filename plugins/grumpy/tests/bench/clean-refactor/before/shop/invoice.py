"""Render a plain-text invoice."""

from .orders import calc


def render(items):
    lines = [f"{qty} x {price}" for price, qty in items]
    lines.append(f"Total: {calc(items)}")
    return "\n".join(lines)
