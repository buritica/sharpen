"""Client for the partner inventory API (limit: 10 requests per second)."""

import json
import urllib.request

BASE_URL = "https://partner.example.com/v1"


def fetch_item(sku):
    with urllib.request.urlopen(f"{BASE_URL}/items/{sku}", timeout=10) as resp:
        return json.load(resp)
