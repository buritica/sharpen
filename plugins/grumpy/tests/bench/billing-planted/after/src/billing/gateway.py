class Gateway:
    """Thin client for the payment provider's REST API."""

    def charge(self, customer_id, amount_cents):
        raise NotImplementedError

    def refund(self, customer_id, amount_cents):
        raise NotImplementedError

    def history(self, customer_id):
        raise NotImplementedError
