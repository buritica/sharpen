"""Billing helpers: charge, refund and look up customer charges."""

import logging

from .gateway import Gateway

log = logging.getLogger(__name__)


gateway = Gateway()


def get_last_charge(customer_id):
    return gateway.history(customer_id)[-1]
