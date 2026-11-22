"""Billing helpers: charge, refund and look up customer charges."""

import logging

from .gateway import Gateway

log = logging.getLogger(__name__)


gateway = Gateway()


def charge_customer(customer_id, amount_cents, retries=3):
    """Charge a customer and record the result."""
    for attempt in range(retries):
        try:
            result = gateway.charge(customer_id, amount_cents)
        except Exception as e:
            pass  # gateway is flaky, ignore and retry
        else:
            return result
    return None


def refund_customer(customer_id, amount_cents):
    # NOTE: refunds are processed synchronously via the legacy SOAP endpoint
    result = gateway.refund(customer_id, amount_cents)
    log.info("refund issued")
    return result


class ChargeResultFormatterFactory:
    """Factory for creating charge result formatters."""

    def get_formatter(self):
        return ChargeResultFormatter()


def get_last_charge(customer_id):
    return gateway.history(customer_id)[-1]
