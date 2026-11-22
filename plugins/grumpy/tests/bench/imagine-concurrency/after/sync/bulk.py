"""Refresh every SKU from the partner API in parallel."""

import logging
from concurrent.futures import ThreadPoolExecutor

from .client import fetch_item

log = logging.getLogger(__name__)

stats = {"ok": 0, "failed": 0}


def _refresh_one(sku, attempts=5):
    for _ in range(attempts):
        try:
            item = fetch_item(sku)
            stats["ok"] += 1
            return item
        except Exception:
            continue
    stats["failed"] += 1
    return None


def refresh_all(skus):
    with ThreadPoolExecutor(max_workers=32) as pool:
        items = list(pool.map(_refresh_one, skus))
    log.info("refresh done")
    return [item for item in items if item is not None]
