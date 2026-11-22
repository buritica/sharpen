### Fix — 2026-11-22

## Fixed
- src/billing/charge.py:18: re-raise the gateway error after the last retry

## Settled
- src/billing/test_charge.py:6 | the smoke test asserts True and tests nothing | declined | the person said the real charge tests land in the follow-up branch that adds the gateway fake
- src/billing/charge.py:28 | ChargeResultFormatterFactory is a factory for a single formatter | declined |
- src/billing/charge.py | stale comments in this file | declined | comments get a separate cleanup pass
